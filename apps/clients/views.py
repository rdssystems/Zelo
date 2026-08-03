from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.accounts.decorators import tenant_admin_required
from apps.accounts.permissions import IsTenantAdminOrReadOnly, IsTenantMember

from . import services as client_ops
from .forms import AddCreditForm, ClientForm, RemoveCreditForm, SubscriptionForm
from .models import Client
from .serializers import (
    AddCreditSerializer,
    ClientCreditTransactionSerializer,
    ClientSerializer,
    RemoveCreditSerializer,
    SetSubscriptionSerializer,
)

# ---------------------------------------------------------------------------
# API REST (DRF) — /api/v1/clients/
# ---------------------------------------------------------------------------


class ClientViewSet(viewsets.ModelViewSet):
    serializer_class = ClientSerializer
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        return Client.objects.for_tenant(self.request.user.tenant)

    def create(self, request, *args, **kwargs):
        data = request.data
        try:
            client = client_ops.create_client(
                tenant=request.user.tenant,
                name=data.get("name", ""),
                phone=data.get("phone", ""),
                preferences=data.get("preferences", ""),
            )
        except ValidationError as exc:
            return Response(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(ClientSerializer(client).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        client = self.get_object()
        data = request.data
        try:
            client_ops.update_client(
                client,
                name=data.get("name", client.name),
                phone=data.get("phone", client.phone),
                preferences=data.get("preferences", client.preferences),
            )
        except ValidationError as exc:
            return Response(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(ClientSerializer(client).data)

    @action(detail=True, methods=["post"], url_path="add-credit")
    def add_credit(self, request, pk=None):
        client = self.get_object()
        serializer = AddCreditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            client_ops.add_client_credit(
                client, created_by=request.user, **serializer.validated_data
            )
        except ValidationError as exc:
            raise DRFValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            )
        return Response(ClientSerializer(client).data)

    @action(detail=True, methods=["post"], url_path="remove-credit")
    def remove_credit(self, request, pk=None):
        client = self.get_object()
        serializer = RemoveCreditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            client_ops.remove_client_credit(
                client, created_by=request.user, **serializer.validated_data
            )
        except ValidationError as exc:
            raise DRFValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            )
        return Response(ClientSerializer(client).data)

    @action(detail=True, methods=["get"], url_path="credit-history")
    def credit_history(self, request, pk=None):
        client = self.get_object()
        entries = client.credit_transactions.all()
        return Response(ClientCreditTransactionSerializer(entries, many=True).data)

    @action(detail=True, methods=["post"], url_path="subscription")
    def subscription(self, request, pk=None):
        client = self.get_object()
        serializer = SetSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            client_ops.set_subscriber_status(client, **serializer.validated_data)
        except ValidationError as exc:
            raise DRFValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            )
        return Response(ClientSerializer(client).data)


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/clientes/
# ---------------------------------------------------------------------------


def _get_client(request, pk):
    return get_object_or_404(Client.objects.for_tenant(request.tenant), pk=pk)


def _default_campaign_message(client, *, overdue):
    """Texto de partida pro cliente editar antes de mandar — não é regra de
    negócio (nada calcula em cima disso), só copy, mesmo tratamento de
    `apps.billing.views.PLAN_HIGHLIGHTS`."""
    due = (
        client.subscription_due_date.strftime("%d/%m/%Y")
        if client.subscription_due_date
        else ""
    )
    if overdue:
        return (
            f"Olá, {client.name}! Vi aqui que sua mensalidade venceu em {due}. "
            "Quando puder, dá uma passadinha pra regularizar — qualquer dúvida, é só chamar "
            "por aqui!"
        )
    return (
        f"Olá, {client.name}! Passando pra lembrar que sua mensalidade vence em {due}. "
        "Já pode se programar — qualquer dúvida, é só chamar por aqui!"
    )


def _campaign_entry(client, *, overdue):
    return {
        "id": client.id,
        "name": client.name,
        "phone": client.phone,
        "due_date": (
            client.subscription_due_date.strftime("%d/%m/%Y")
            if client.subscription_due_date
            else ""
        ),
        "message": _default_campaign_message(client, overdue=overdue),
    }


@tenant_admin_required
def subscription_whatsapp_campaign(request):
    """Modal da campanha de cobrança/lembrete por WhatsApp (`/painel/clientes/
    mensalistas/whatsapp/`) — lista mensalista vencido ou a vencer (janela de
    `Tenant.subscription_due_soon_days`), cada um com número + mensagem
    pronta (editável) pra abrir o WhatsApp um a um. Só entram clientes com
    telefone válido pra link (`whatsapp_url`/`phone.isdigit()`) — anonimizado
    via LGPD não tem como ser contatado."""
    clients = [
        c
        for c in Client.objects.for_tenant(request.tenant)
        .select_related("tenant")
        .filter(is_subscriber=True)
        if c.phone.isdigit()
    ]
    return render(
        request,
        "painel/clients/_whatsapp_campaign_modal.html",
        {
            "overdue_clients": [
                _campaign_entry(c, overdue=True) for c in clients if c.subscription_is_overdue
            ],
            "due_soon_clients": [
                _campaign_entry(c, overdue=False)
                for c in clients
                if c.subscription_is_due_soon
            ],
            "due_soon_days": request.tenant.subscription_due_soon_days,
        },
    )


def _history_context(client):
    appointments = (
        client.appointments.select_related("service", "employee")
        .prefetch_related("stock_movements__product")
        .order_by("-date", "-start_time")
    )
    return appointments


@tenant_admin_required
def client_list(request):
    clients = Client.objects.for_tenant(request.tenant).select_related("tenant")
    query = request.GET.get("q", "").strip()
    if query:
        clients = clients.filter(name__icontains=query) | clients.filter(
            phone__icontains=query
        )
    return render(
        request,
        "painel/clients/list.html",
        {"clients": clients.order_by("name"), "query": query, "active_nav": "clients"},
    )


@tenant_admin_required
def client_create(request):
    if request.method == "POST":
        form = ClientForm(request.POST)
        if form.is_valid():
            try:
                client = client_ops.create_client(
                    tenant=request.tenant, **form.cleaned_data
                )
            except ValidationError as exc:
                for field, errors in getattr(
                    exc, "message_dict", {"__all__": exc.messages}
                ).items():
                    for error in errors:
                        form.add_error(field if field in form.fields else None, error)
            else:
                messages.success(request, "Cliente cadastrado.")
                return redirect("clients:update", pk=client.pk)
    else:
        form = ClientForm()
    return render(
        request,
        "painel/clients/form.html",
        {"form": form, "active_nav": "clients", "client": None},
    )


@tenant_admin_required
def client_update(request, pk):
    client = _get_client(request, pk)
    if request.method == "POST":
        form = ClientForm(
            request.POST,
            initial={
                "name": client.name,
                "phone": client.phone,
                "preferences": client.preferences,
            },
        )
        if form.is_valid():
            try:
                client_ops.update_client(client, **form.cleaned_data)
            except ValidationError as exc:
                for field, errors in getattr(
                    exc, "message_dict", {"__all__": exc.messages}
                ).items():
                    for error in errors:
                        form.add_error(field if field in form.fields else None, error)
            else:
                messages.success(request, "Dados do cliente atualizados.")
                return redirect("clients:update", pk=client.pk)
    else:
        form = ClientForm(
            initial={
                "name": client.name,
                "phone": client.phone,
                "preferences": client.preferences,
            }
        )
    return render(
        request,
        "painel/clients/form.html",
        {
            "form": form,
            "client": client,
            "active_nav": "clients",
            "history": _history_context(client),
            "subscription_form": SubscriptionForm(
                initial={
                    "is_subscriber": client.is_subscriber,
                    "subscription_due_date": client.subscription_due_date,
                }
            ),
        },
    )


@tenant_admin_required
def client_delete_confirm(request, pk):
    client = _get_client(request, pk)
    return render(
        request,
        "painel/_confirm_delete.html",
        {
            "title": "Excluir cliente",
            "item_label": client.name,
            "warning": (
                "O nome e o telefone deste cliente serão apagados (ele some da lista e "
                "não pode mais ser identificado). O histórico de atendimentos, crédito e "
                "transações financeiras já registrados NÃO é afetado — continua contando "
                "para o caixa e os relatórios normalmente."
            ),
            "delete_url": reverse("clients:delete", args=[client.pk]),
            "target": "body",
        },
    )


@tenant_admin_required
def client_delete(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    client = _get_client(request, pk)
    client_ops.anonymize_client(client)
    response = HttpResponse()
    response.headers["HX-Redirect"] = reverse("clients:list")
    return response


@tenant_admin_required
def client_subscription(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    client = _get_client(request, pk)
    form = SubscriptionForm(request.POST)
    error = None
    if form.is_valid():
        try:
            client_ops.set_subscriber_status(
                client,
                is_subscriber=form.cleaned_data["is_subscriber"],
                due_date=form.cleaned_data["subscription_due_date"],
            )
        except ValidationError as exc:
            error = " ".join(exc.messages)
    else:
        error = " ".join(
            msg for errors in form.errors.values() for msg in errors
        )
    client.refresh_from_db()
    return render(
        request,
        "painel/clients/_subscription.html",
        {
            "client": client,
            "subscription_form": SubscriptionForm(
                initial={
                    "is_subscriber": client.is_subscriber,
                    "subscription_due_date": client.subscription_due_date,
                }
            ),
            "subscription_error": error,
            "subscription_saved": error is None,
        },
    )


@tenant_admin_required
def client_renew_subscription(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    client = _get_client(request, pk)
    try:
        client_ops.renew_subscription(client)
    except ValidationError:
        return HttpResponse(status=409)
    client.refresh_from_db()
    return render(
        request,
        "painel/clients/_subscription.html",
        {
            "client": client,
            "subscription_form": SubscriptionForm(
                initial={
                    "is_subscriber": client.is_subscriber,
                    "subscription_due_date": client.subscription_due_date,
                }
            ),
            "subscription_error": None,
            "subscription_saved": True,
        },
    )


def _credit_response(request, client):
    """Recarrega o painel de crédito (saldo + histórico) e fecha o modal —
    mesmo padrão de `_items_response` usado nos outros apps."""
    client.refresh_from_db()
    panel = render_to_string(
        "painel/clients/_credit.html", {"client": client}, request=request
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(panel + modal_reset)


@tenant_admin_required
def client_credit_add_confirm(request, pk):
    client = _get_client(request, pk)
    return render(
        request,
        "painel/clients/_credit_add_form.html",
        {"client": client, "form": AddCreditForm()},
    )


@tenant_admin_required
def client_credit_add(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    client = _get_client(request, pk)
    form = AddCreditForm(request.POST)
    if form.is_valid():
        client_ops.add_client_credit(
            client, created_by=request.user, **form.cleaned_data
        )
        return _credit_response(request, client)
    response = render(
        request,
        "painel/clients/_credit_add_form.html",
        {"client": client, "form": form},
    )
    response.headers["HX-Retarget"] = "#modal-slot"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def client_credit_remove_confirm(request, pk):
    client = _get_client(request, pk)
    return render(
        request,
        "painel/clients/_credit_remove_form.html",
        {"client": client, "form": RemoveCreditForm()},
    )


@tenant_admin_required
def client_credit_remove(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    client = _get_client(request, pk)
    form = RemoveCreditForm(request.POST)
    if form.is_valid():
        try:
            client_ops.remove_client_credit(
                client,
                created_by=request.user,
                amount=form.cleaned_data["amount"],
                reason=form.cleaned_data["reason"] or "Ajuste manual",
            )
        except ValidationError as exc:
            form.add_error("amount", " ".join(exc.messages))
        else:
            return _credit_response(request, client)
    response = render(
        request,
        "painel/clients/_credit_remove_form.html",
        {"client": client, "form": form},
    )
    response.headers["HX-Retarget"] = "#modal-slot"
    response.headers["HX-Reswap"] = "innerHTML"
    return response
