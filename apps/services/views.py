from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.accounts.decorators import tenant_admin_required
from apps.accounts.permissions import IsTenantAdminOrReadOnly, IsTenantMember
from apps.inventory.models import Product

from . import services as service_ops
from .forms import ServiceForm
from .models import Service, ServiceProduct
from .serializers import ServiceSerializer

# ---------------------------------------------------------------------------
# API REST (DRF) — /api/v1/services/  (espelha o painel; futura app mobile)
# ---------------------------------------------------------------------------


class ServiceViewSet(viewsets.ModelViewSet):
    # Na API usamos request.user.tenant (e não request.tenant): a autenticação
    # DRF (sessão hoje, token/JWT no futuro) acontece DEPOIS do middleware de
    # tenant, então request.tenant não é confiável neste contexto.
    serializer_class = ServiceSerializer
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]

    def get_queryset(self):
        return Service.objects.for_tenant(self.request.user.tenant)

    def perform_create(self, serializer):
        data = dict(serializer.validated_data)
        is_active = data.pop("is_active", True)
        service = service_ops.create_service(
            tenant=self.request.user.tenant, **data
        )
        if not is_active:
            service_ops.set_service_active(service, False)
        serializer.instance = service

    def perform_update(self, serializer):
        data = {
            "name": serializer.validated_data.get("name", serializer.instance.name),
            "description": serializer.validated_data.get(
                "description", serializer.instance.description
            ),
            "duration_minutes": serializer.validated_data.get(
                "duration_minutes", serializer.instance.duration_minutes
            ),
            "price": serializer.validated_data.get(
                "price", serializer.instance.price
            ),
        }
        service_ops.update_service(serializer.instance, **data)
        if "is_active" in serializer.validated_data:
            service_ops.set_service_active(
                serializer.instance, serializer.validated_data["is_active"]
            )

    def perform_destroy(self, instance):
        try:
            service_ops.delete_service(instance)
        except ValidationError as exc:
            raise DRFValidationError(exc.messages)


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/servicos/
# ---------------------------------------------------------------------------


def _get_service(request, pk):
    return get_object_or_404(
        Service.objects.for_tenant(request.tenant), pk=pk
    )


def _items_response(request, extra_html=""):
    """Lista atualizada + limpeza do modal (swap out-of-band do HTMX)."""
    bookable_ids = set(
        service_ops.bookable_services(request.tenant).values_list("pk", flat=True)
    )
    items = render_to_string(
        "painel/services/_items.html",
        {"services": Service.objects.for_tenant(request.tenant), "bookable_ids": bookable_ids},
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset + extra_html)


@tenant_admin_required
def service_list(request):
    services = Service.objects.for_tenant(request.tenant)
    # Serviço ativo sem funcionário vinculado não aparece na página pública
    # (RF14, `bookable_services`) — mas o admin ainda consegue agendar com
    # ele manualmente (RF17, "encaixe" não exige o vínculo). Avisar aqui
    # evita o admin descobrir esse gap só quando um cliente reclamar que não
    # achou o serviço pra agendar sozinho.
    bookable_ids = set(
        service_ops.bookable_services(request.tenant).values_list("pk", flat=True)
    )
    return render(
        request,
        "painel/services/list.html",
        {"services": services, "bookable_ids": bookable_ids, "active_nav": "services"},
    )


@tenant_admin_required
def service_create(request):
    if request.method == "POST":
        form = ServiceForm(request.POST)
        if form.is_valid():
            service_ops.create_service(
                tenant=request.tenant, **form.cleaned_data
            )
            return _items_response(request)
    else:
        form = ServiceForm()
    response = render(
        request,
        "painel/services/_form.html",
        {"form": form, "title": "Novo Serviço", "post_url_name": "create"},
    )
    if request.method == "POST":
        # formulário inválido: reabre o modal em vez de trocar a lista
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def service_update(request, pk):
    service = _get_service(request, pk)
    if request.method == "POST":
        form = ServiceForm(request.POST, instance=service)
        if form.is_valid():
            service_ops.update_service(service, **form.cleaned_data)
            return _items_response(request)
    else:
        form = ServiceForm(instance=service)
    response = render(
        request,
        "painel/services/_form.html",
        {
            "form": form,
            "title": "Editar Serviço",
            "post_url_name": "update",
            "service": service,
        },
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def service_toggle_confirm(request, pk):
    service = _get_service(request, pk)
    if service.is_active:
        context = {
            "title": "Desativar serviço",
            "message": (
                f"Desativar '{service.name}'? Ele deixa de aparecer na página "
                f"pública de agendamento."
            ),
            "icon": "toggle_off",
            "confirm_label": "Desativar",
        }
    else:
        context = {
            "title": "Reativar serviço",
            "message": f"Reativar '{service.name}'? Ele volta a aparecer na página pública.",
            "icon": "toggle_on",
            "confirm_label": "Reativar",
        }
    context.update(
        {
            "action_url": reverse("services:toggle", args=[service.pk]),
            "target": "#service-items",
        }
    )
    return render(request, "painel/_confirm_action.html", context)


@tenant_admin_required
def service_toggle(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    service = _get_service(request, pk)
    service_ops.set_service_active(service, not service.is_active)
    return _items_response(request)


@tenant_admin_required
def service_delete_confirm(request, pk):
    service = _get_service(request, pk)
    return render(
        request,
        "painel/_confirm_delete.html",
        {
            "title": "Excluir serviço",
            "item_label": service.name,
            "warning": "Ele deixará de existir no catálogo do salão.",
            "delete_url": reverse("services:delete", args=[service.pk]),
            "target": "#service-items",
        },
    )


@tenant_admin_required
def service_delete(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    service = _get_service(request, pk)
    try:
        service_ops.delete_service(service)
    except ValidationError as exc:
        response = render(
            request,
            "painel/_modal_error.html",
            {"title": "Não foi possível excluir", "message": " ".join(exc.messages)},
        )
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
        return response
    return _items_response(request)


# ---------------------------------------------------------------------------
# Receita do serviço (RF48) — insumo consumido automaticamente do estoque a
# cada atendimento concluído, sem cobrar o cliente. Mesmo padrão de
# add/editar quantidade/remover que apps.finance.views usa pra
# ComandaProductItem, trocando cliente por serviço.
# ---------------------------------------------------------------------------


def _grouped_products_for_recipe(tenant):
    """Igual a `apps.finance.views._grouped_products`, mas SEM filtro
    `is_for_sale` — qualquer produto ativo (vendável ou insumo puro) pode
    virar ingrediente de receita. Não é importado de lá pra evitar import
    circular (apps.finance já importa de apps.services)."""
    products = (
        Product.objects.for_tenant(tenant)
        .filter(is_active=True)
        .select_related("category")
        .order_by("name")
    )
    grouped = {}
    uncategorized = []
    for product in products:
        if product.category_id:
            grouped.setdefault(product.category, []).append(product)
        else:
            uncategorized.append(product)
    categories_with_products = [
        {"category": category, "products": items}
        for category, items in sorted(grouped.items(), key=lambda row: row[0].name)
    ]
    return categories_with_products, uncategorized


def _recipe_items_response(request, service):
    items = render_to_string(
        "painel/services/_recipe_items.html",
        {"service": service, "recipe_items": service.recipe_items.select_related("product")},
        request=request,
    )
    return HttpResponse(items)


@tenant_admin_required
def service_recipe(request, pk):
    service = _get_service(request, pk)
    return render(
        request,
        "painel/services/_recipe_modal.html",
        {"service": service, "recipe_items": service.recipe_items.select_related("product")},
    )


@tenant_admin_required
def service_recipe_product_picker(request, pk):
    service = _get_service(request, pk)
    categories_with_products, uncategorized_products = _grouped_products_for_recipe(request.tenant)
    return render(
        request,
        "painel/services/_recipe_product_picker.html",
        {
            "service": service,
            "categories_with_products": categories_with_products,
            "uncategorized_products": uncategorized_products,
        },
    )


@tenant_admin_required
def service_recipe_item_add(request, pk):
    """Diferente de `comanda_item_add` (que troca só a listinha por baixo do
    picker, na página principal): aqui o picker TOMOU o `#modal-slot`
    inteiro (não tem "por baixo"), então adicionar volta pro modal da
    receita completo — mesma renderização de `service_recipe`."""
    if request.method != "POST":
        return HttpResponse(status=405)
    service = _get_service(request, pk)
    product = get_object_or_404(
        Product.objects.for_tenant(request.tenant),
        pk=request.POST.get("product_id"), is_active=True,
    )
    try:
        service_ops.add_recipe_item(service=service, product=product, quantity=Decimal("1"))
    except ValidationError:
        return HttpResponse(status=409)
    return render(
        request,
        "painel/services/_recipe_modal.html",
        {"service": service, "recipe_items": service.recipe_items.select_related("product")},
    )


@tenant_admin_required
def service_recipe_item_update(request, item_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    item = get_object_or_404(
        ServiceProduct.objects.for_tenant(request.tenant).select_related("service"),
        pk=item_id,
    )
    try:
        quantity = Decimal(str(request.POST.get("quantity", "0")).strip().replace(",", "."))
        service_ops.set_recipe_item_quantity(item, quantity)
    except (ValidationError, InvalidOperation):
        return HttpResponse(status=409)
    return _recipe_items_response(request, item.service)


@tenant_admin_required
def service_recipe_item_remove(request, item_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    item = get_object_or_404(
        ServiceProduct.objects.for_tenant(request.tenant).select_related("service"), pk=item_id
    )
    service = item.service
    service_ops.remove_recipe_item(item)
    return _recipe_items_response(request, service)
