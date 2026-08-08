import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Count, F, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from rest_framework import mixins, viewsets
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.accounts.decorators import tenant_admin_required
from apps.accounts.permissions import IsTenantAdminOrReadOnly, IsTenantMember

from . import services as inventory_ops
from .forms import CategoryForm, ProductForm, StockMovementForm, SupplierForm
from .models import Category, InventoryCountStatus, PhysicalInventoryCount, Product, Supplier
from .serializers import (
    ProductSerializer,
    StockMovementCreateSerializer,
    StockMovementSerializer,
    SupplierSerializer,
)

# ---------------------------------------------------------------------------
# API REST (DRF) — /api/v1/products/ e /api/v1/stock-movements/
# ---------------------------------------------------------------------------


class ProductViewSet(viewsets.ModelViewSet):
    # request.user.tenant (não request.tenant) — ver comentário em
    # apps/services/views.py sobre a ordem middleware x autenticação DRF.
    serializer_class = ProductSerializer
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]

    def get_queryset(self):
        return Product.objects.for_tenant(self.request.user.tenant)

    def _validate_category_tenant(self, category):
        if category is not None and category.tenant_id != self.request.user.tenant_id:
            raise DRFValidationError({"category": "Categoria não pertence a este tenant."})

    def _validate_supplier_tenant(self, supplier):
        if supplier is not None and supplier.tenant_id != self.request.user.tenant_id:
            raise DRFValidationError({"supplier": "Fornecedor não pertence a este tenant."})

    def perform_create(self, serializer):
        self._validate_category_tenant(serializer.validated_data.get("category"))
        self._validate_supplier_tenant(serializer.validated_data.get("supplier"))
        serializer.instance = inventory_ops.create_product(
            tenant=self.request.user.tenant, **serializer.validated_data
        )

    def perform_update(self, serializer):
        data = serializer.validated_data
        self._validate_category_tenant(data.get("category"))
        self._validate_supplier_tenant(data.get("supplier"))
        inventory_ops.update_product(
            serializer.instance,
            name=data.get("name", serializer.instance.name),
            sku=data.get("sku", serializer.instance.sku),
            category=data.get("category", serializer.instance.category),
            supplier=data.get("supplier", serializer.instance.supplier),
            unit=data.get("unit", serializer.instance.unit),
            cost_price=data.get("cost_price", serializer.instance.cost_price),
            sale_price=data.get("sale_price", serializer.instance.sale_price),
            min_stock_alert=data.get(
                "min_stock_alert", serializer.instance.min_stock_alert
            ),
            tracks_batches=data.get(
                "tracks_batches", serializer.instance.tracks_batches
            ),
        )

    def perform_destroy(self, instance):
        try:
            inventory_ops.delete_product(instance)
        except ValidationError as exc:
            raise DRFValidationError(exc.messages)


class StockMovementViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]

    def get_queryset(self):
        from .models import StockMovement

        return StockMovement.objects.for_tenant(self.request.user.tenant).select_related(
            "product"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return StockMovementCreateSerializer
        return StockMovementSerializer

    def perform_create(self, serializer):
        data = dict(serializer.validated_data)
        product = data.pop("product")
        if product.tenant_id != self.request.user.tenant_id:
            raise DRFValidationError("Produto não pertence a este tenant.")
        try:
            movement = inventory_ops.register_stock_movement(
                tenant=self.request.user.tenant,
                created_by=self.request.user,
                product=product,
                movement_type=data.pop("type"),
                **data,
            )
        except ValidationError as exc:
            raise DRFValidationError(exc.messages)
        serializer.instance = movement


class SupplierViewSet(viewsets.ModelViewSet):
    serializer_class = SupplierSerializer
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]

    def get_queryset(self):
        return Supplier.objects.for_tenant(self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.instance = inventory_ops.create_supplier(
            tenant=self.request.user.tenant, **serializer.validated_data
        )

    def perform_update(self, serializer):
        data = serializer.validated_data
        inventory_ops.update_supplier(
            serializer.instance,
            name=data.get("name", serializer.instance.name),
            contact_name=data.get("contact_name", serializer.instance.contact_name),
            phone=data.get("phone", serializer.instance.phone),
            email=data.get("email", serializer.instance.email),
            notes=data.get("notes", serializer.instance.notes),
        )


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/estoque/
# ---------------------------------------------------------------------------


def _get_product(request, pk):
    return get_object_or_404(Product.objects.for_tenant(request.tenant), pk=pk)


def _stock_stats(tenant, products):
    total_value = products.aggregate(
        total=Sum(F("current_stock") * F("cost_price"))
    )["total"] or Decimal("0")
    # current_stock * cost_price soma as casas decimais dos dois campos
    # (2 + 2 = 4) — arredonda de volta para valor monetário (2 casas).
    total_value = total_value.quantize(Decimal("0.01"))
    return {
        "total_items": products.count(),
        "low_stock_count": sum(1 for p in products if p.is_low_stock),
        "total_stock_value": total_value,
        "expiring_batches_count": inventory_ops.batches_expiring_soon(tenant).count(),
    }


def _filtered_products(request):
    products = Product.objects.for_tenant(request.tenant).select_related("category")
    category_id = request.GET.get("category")
    if category_id:
        products = products.filter(category_id=category_id)
    situacao = request.GET.get("situacao")
    if situacao == "low_stock":
        products = products.filter(current_stock__lte=F("min_stock_alert"))
    elif situacao == "expiring_batches":
        # Mesmo critério do card "Lotes Vencendo" (inventory_ops.batches_expiring_soon)
        # — reaproveita em vez de duplicar a janela de 30 dias aqui.
        expiring_product_ids = inventory_ops.batches_expiring_soon(request.tenant).values_list(
            "product_id", flat=True
        )
        products = products.filter(id__in=expiring_product_ids)
    return products


def _items_response(request):
    products = _filtered_products(request)
    items = render_to_string(
        "painel/inventory/_items.html",
        {
            "products": products,
            "categories": Category.objects.for_tenant(request.tenant),
            "selected_category": request.GET.get("category", ""),
            "selected_situacao": request.GET.get("situacao", ""),
            **_stock_stats(request.tenant, products),
        },
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset)


@tenant_admin_required
def product_list(request):
    products = _filtered_products(request)
    return render(
        request,
        "painel/inventory/list.html",
        {
            "products": products,
            "categories": Category.objects.for_tenant(request.tenant),
            "selected_category": request.GET.get("category", ""),
            "selected_situacao": request.GET.get("situacao", ""),
            "active_nav": "inventory",
            **_stock_stats(request.tenant, products),
        },
    )


@tenant_admin_required
def product_create(request):
    if request.method == "POST":
        form = ProductForm(request.POST, tenant=request.tenant)
        if form.is_valid():
            inventory_ops.create_product(tenant=request.tenant, **form.cleaned_data)
            return _items_response(request)
    else:
        form = ProductForm(tenant=request.tenant)
    response = render(
        request,
        "painel/inventory/_form.html",
        {"form": form, "title": "Novo Produto"},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def product_update(request, pk):
    product = _get_product(request, pk)
    cost_price_locked = product.has_purchase_history
    if request.method == "POST":
        form = ProductForm(
            request.POST,
            tenant=request.tenant,
            cost_price_locked=cost_price_locked,
            initial={"cost_price": product.cost_price},
        )
        if form.is_valid():
            inventory_ops.update_product(product, **form.cleaned_data)
            return _items_response(request)
    else:
        form = ProductForm(
            tenant=request.tenant,
            cost_price_locked=cost_price_locked,
            initial={
                "name": product.name,
                "sku": product.sku,
                "category": product.category_id,
                "supplier": product.supplier_id,
                "unit": product.unit,
                "cost_price": product.cost_price,
                "sale_price": product.sale_price,
                "min_stock_alert": product.min_stock_alert,
                "tracks_batches": product.tracks_batches,
            },
        )
    response = render(
        request,
        "painel/inventory/_form.html",
        {"form": form, "title": "Editar Produto", "product": product},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def product_toggle_confirm(request, pk):
    product = _get_product(request, pk)
    if product.is_active:
        context = {
            "title": "Desativar produto",
            "message": f"Desativar '{product.name}'? Ele deixa de aparecer disponível para movimentação.",
            "icon": "toggle_off",
            "confirm_label": "Desativar",
        }
    else:
        context = {
            "title": "Reativar produto",
            "message": f"Reativar '{product.name}'?",
            "icon": "toggle_on",
            "confirm_label": "Reativar",
        }
    context.update(
        {"action_url": reverse("inventory:toggle", args=[product.pk]), "target": "#product-items"}
    )
    return render(request, "painel/_confirm_action.html", context)


@tenant_admin_required
def product_toggle(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    product = _get_product(request, pk)
    inventory_ops.set_product_active(product, not product.is_active)
    return _items_response(request)


@tenant_admin_required
def product_delete_confirm(request, pk):
    product = _get_product(request, pk)
    return render(
        request,
        "painel/_confirm_delete.html",
        {
            "title": "Excluir produto",
            "item_label": product.name,
            "warning": "Ele deixará de existir no catálogo de estoque.",
            "delete_url": reverse("inventory:delete", args=[product.pk]),
            "target": "#product-items",
        },
    )


@tenant_admin_required
def product_delete(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    product = _get_product(request, pk)
    try:
        inventory_ops.delete_product(product)
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


@tenant_admin_required
def product_movement(request, pk):
    product = _get_product(request, pk)
    if request.method == "POST":
        form = StockMovementForm(request.POST, tenant=request.tenant)
        if form.is_valid():
            try:
                inventory_ops.register_stock_movement(
                    tenant=request.tenant,
                    product=product,
                    movement_type=form.cleaned_data["type"],
                    quantity=form.cleaned_data["quantity"],
                    unit_price=form.cleaned_data["unit_price"],
                    reason=form.cleaned_data["reason"],
                    supplier=form.cleaned_data["supplier"],
                    batch_number=form.cleaned_data["batch_number"],
                    expiry_date=form.cleaned_data["expiry_date"],
                    created_by=request.user,
                )
            except ValidationError as exc:
                # Mesmo padrão de apps.employees.views.employee_create — erro
                # com campo (ex.: "expiry_date" faltando) vira erro INLINE
                # nesse campo do form; erro sem campo (ex.: "estoque
                # insuficiente") cai em non_field_errors, já tratado no
                # template.
                for field, errors in getattr(exc, "message_dict", {"__all__": exc.messages}).items():
                    for message in errors:
                        form.add_error(field if field in form.fields else None, message)
            else:
                return _items_response(request)
    else:
        form = StockMovementForm(
            tenant=request.tenant,
            initial={
                "unit_price": product.cost_price, "type": "in", "reason": "purchase",
                "supplier": product.supplier_id,
            },
        )
    response = render(
        request,
        "painel/inventory/_movement_form.html",
        {"form": form, "product": product},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def product_batches(request, pk):
    """Lista de lotes de um produto (RF44) — mais próximo do vencimento primeiro."""
    product = _get_product(request, pk)
    batches = product.batches.select_related("supplier").order_by("expiry_date")
    return render(
        request,
        "painel/inventory/_batches_list.html",
        {"product": product, "batches": batches, "today": datetime.date.today()},
    )


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/estoque/categorias/
# ---------------------------------------------------------------------------


def _get_category(request, pk):
    return get_object_or_404(Category.objects.for_tenant(request.tenant), pk=pk)


def _categories_with_product_count(request):
    return Category.objects.for_tenant(request.tenant).annotate(product_count=Count("products"))


def _category_items_response(request):
    items = render_to_string(
        "painel/inventory/categories/_items.html",
        {"categories": _categories_with_product_count(request)},
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset)


@tenant_admin_required
def category_list(request):
    return render(
        request,
        "painel/inventory/categories/list.html",
        {"categories": _categories_with_product_count(request), "active_nav": "inventory"},
    )


@tenant_admin_required
def category_create(request):
    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            inventory_ops.create_category(tenant=request.tenant, **form.cleaned_data)
            return _category_items_response(request)
    else:
        form = CategoryForm()
    response = render(
        request,
        "painel/inventory/categories/_form.html",
        {"form": form, "title": "Nova Categoria"},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def category_update(request, pk):
    category = _get_category(request, pk)
    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            inventory_ops.update_category(category, **form.cleaned_data)
            return _category_items_response(request)
    else:
        form = CategoryForm(initial={"name": category.name})
    response = render(
        request,
        "painel/inventory/categories/_form.html",
        {"form": form, "title": "Editar Categoria", "category": category},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def category_delete_confirm(request, pk):
    category = _get_category(request, pk)
    return render(
        request,
        "painel/_confirm_delete.html",
        {
            "title": "Excluir categoria",
            "item_label": category.name,
            "warning": "Só é possível excluir categorias sem produtos vinculados.",
            "delete_url": reverse("inventory:category_delete", args=[category.pk]),
            "target": "#category-items",
        },
    )


@tenant_admin_required
def category_delete(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    category = _get_category(request, pk)
    try:
        inventory_ops.delete_category(category)
    except ValidationError:
        return HttpResponse(status=409)
    return _category_items_response(request)


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/estoque/fornecedores/ (RF43)
# ---------------------------------------------------------------------------


def _get_supplier(request, pk):
    return get_object_or_404(Supplier.objects.for_tenant(request.tenant), pk=pk)


def _supplier_items_response(request):
    items = render_to_string(
        "painel/inventory/suppliers/_items.html",
        {"suppliers": Supplier.objects.for_tenant(request.tenant).annotate(
            product_count=Count("products")
        )},
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset)


@tenant_admin_required
def supplier_list(request):
    return render(
        request,
        "painel/inventory/suppliers/list.html",
        {
            "suppliers": Supplier.objects.for_tenant(request.tenant).annotate(
                product_count=Count("products")
            ),
            "active_nav": "inventory",
        },
    )


@tenant_admin_required
def supplier_create(request):
    if request.method == "POST":
        form = SupplierForm(request.POST)
        if form.is_valid():
            inventory_ops.create_supplier(tenant=request.tenant, **form.cleaned_data)
            return _supplier_items_response(request)
    else:
        form = SupplierForm()
    response = render(
        request, "painel/inventory/suppliers/_form.html", {"form": form, "title": "Novo Fornecedor"}
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def supplier_update(request, pk):
    supplier = _get_supplier(request, pk)
    if request.method == "POST":
        form = SupplierForm(request.POST)
        if form.is_valid():
            inventory_ops.update_supplier(supplier, **form.cleaned_data)
            return _supplier_items_response(request)
    else:
        form = SupplierForm(
            initial={
                "name": supplier.name, "contact_name": supplier.contact_name,
                "phone": supplier.phone, "email": supplier.email, "notes": supplier.notes,
            }
        )
    response = render(
        request,
        "painel/inventory/suppliers/_form.html",
        {"form": form, "title": "Editar Fornecedor", "supplier": supplier},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def supplier_toggle(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    supplier = _get_supplier(request, pk)
    inventory_ops.set_supplier_active(supplier, not supplier.is_active)
    return _supplier_items_response(request)


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/estoque/inventario/ (RF46)
# ---------------------------------------------------------------------------


def _get_count(request, pk):
    return get_object_or_404(PhysicalInventoryCount.objects.for_tenant(request.tenant), pk=pk)


def _get_count_item(request, count, pk):
    return get_object_or_404(count.items.filter(tenant=request.tenant), pk=pk)


@tenant_admin_required
def inventory_count_list(request):
    return render(
        request,
        "painel/inventory/counts/list.html",
        {
            "counts": PhysicalInventoryCount.objects.for_tenant(request.tenant).select_related(
                "created_by"
            ),
            "active_nav": "inventory",
        },
    )


@tenant_admin_required
def inventory_count_start(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    count = inventory_ops.start_inventory_count(
        tenant=request.tenant, created_by=request.user, notes=request.POST.get("notes", "")
    )
    return redirect("inventory:count_detail", pk=count.pk)


@tenant_admin_required
def inventory_count_detail(request, pk):
    count = _get_count(request, pk)
    return render(
        request,
        "painel/inventory/counts/detail.html",
        {
            "count": count,
            "items": count.items.select_related("product"),
            "active_nav": "inventory",
        },
    )


def _count_items_response(request, count):
    items = render_to_string(
        "painel/inventory/counts/_items.html",
        {"count": count, "items": count.items.select_related("product")},
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset)


@tenant_admin_required
def inventory_count_item_update(request, pk, item_pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    count = _get_count(request, pk)
    item = _get_count_item(request, count, item_pk)
    raw = (request.POST.get("counted_quantity") or "").strip().replace(",", ".")
    try:
        inventory_ops.set_counted_quantity(item, raw or None)
    except ValidationError:
        return HttpResponse(status=409)
    return _count_items_response(request, count)


@tenant_admin_required
def inventory_count_close_confirm(request, pk):
    count = _get_count(request, pk)
    pending = count.items.filter(counted_quantity__isnull=True).count()
    message = "Fechar esta contagem? Toda divergência vira uma movimentação de ajuste no estoque."
    if pending:
        message += f" {pending} produto(s) ainda sem quantidade contada — serão ignorados."
    return render(
        request,
        "painel/_confirm_action.html",
        {
            "title": "Fechar contagem",
            "message": message,
            "icon": "fact_check",
            "confirm_label": "Fechar contagem",
            "action_url": reverse("inventory:count_close", args=[count.pk]),
            "target": "#count-detail",
        },
    )


@tenant_admin_required
def inventory_count_close(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    count = _get_count(request, pk)
    try:
        inventory_ops.close_inventory_count(count, created_by=request.user)
    except ValidationError:
        return HttpResponse(status=409)
    html = render_to_string(
        "painel/inventory/counts/_detail_body.html",
        {"count": count, "items": count.items.select_related("product")},
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(html + modal_reset)
