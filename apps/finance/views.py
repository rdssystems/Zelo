import datetime
from decimal import Decimal, InvalidOperation
from itertools import groupby

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.accounts.decorators import employee_required, tenant_admin_required
from apps.accounts.permissions import IsTenantAdminOrReadOnly, IsTenantMember
from apps.clients.models import Client
from apps.employees.models import Employee
from apps.inventory.models import WHOLE_UNIT_CODES, Product
from apps.scheduling.models import Appointment, AppointmentStatus
from apps.scheduling.services import (
    build_product_usage,
    complete_client_comanda,
    remove_appointment_from_comanda,
    start_walk_in_service,
)
from apps.services.models import Service
from apps.services.services import bookable_services

from . import services as finance_ops
from .forms import REAL_MONEY_METHODS, ExpenseCategoryForm, ExpenseForm, PayCommissionForm
from .models import CashTransaction, ComandaProductItem, Commission, CommissionStatus, ExpenseCategory
from .serializers import (
    CashTransactionSerializer,
    CommissionSerializer,
    ExpenseCreateSerializer,
    PayCommissionSerializer,
)

# ---------------------------------------------------------------------------
# API REST (DRF) — /api/v1/cash-transactions/ e /api/v1/commissions/
# ---------------------------------------------------------------------------


class CashTransactionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]

    def get_queryset(self):
        return CashTransaction.objects.for_tenant(self.request.user.tenant).order_by(
            "-created_at"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ExpenseCreateSerializer
        return CashTransactionSerializer

    def perform_create(self, serializer):
        transaction = finance_ops.create_expense(
            tenant=self.request.user.tenant,
            created_by=self.request.user,
            **serializer.validated_data,
        )
        serializer.instance = transaction


class CommissionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = CommissionSerializer
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]

    def get_queryset(self):
        return Commission.objects.for_tenant(self.request.user.tenant).select_related(
            "employee", "appointment"
        )

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        commission = self.get_object()
        serializer = PayCommissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            finance_ops.mark_commission_paid(
                commission,
                payment_method=serializer.validated_data["payment_method"],
                created_by=request.user,
            )
        except ValidationError as exc:
            raise DRFValidationError(exc.messages)
        return Response(CommissionSerializer(commission).data)


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/caixa/
# ---------------------------------------------------------------------------


def _parse_date(raw, default):
    try:
        return datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        return default


def _commissions_by_employee(tenant, start, end):
    """Agrupa as comissões (pendentes E pagas) do período por funcionário,
    pra aba Comissões do Caixa — cada grupo sabe se tem alguma pendente
    (pra mostrar o botão "Pagar tudo") e o valor total pendente.

    Filtra por `created_at` (quando a comissão foi gerada), não por
    `appointment__date` (data agendada do serviço) — mesmo critério de
    `period_summary` (`CashTransaction.created_at`), senão um atendimento
    concluído antes/depois da data agendada some daqui mas continua contando
    no saldo do período, inconsistência real já vista em produção."""
    commissions = (
        Commission.objects.for_tenant(tenant)
        .filter(created_at__date__range=(start, end))
        .select_related("employee", "appointment__service")
        .order_by("employee__full_name", "created_at")
    )
    groups = []
    for employee, rows in groupby(commissions, key=lambda c: c.employee):
        rows = list(rows)
        pending_total = sum(
            (c.calculated_amount for c in rows if c.status == CommissionStatus.PENDING),
            Decimal("0.00"),
        )
        groups.append(
            {
                "employee": employee,
                "commissions": rows,
                "has_pending": any(c.status == CommissionStatus.PENDING for c in rows),
                "pending_total": pending_total,
            }
        )
    return groups


def _parse_decimal(raw, field="valor"):
    """Valor opcional digitado pelo admin (aceita vírgula) — em branco/None
    vira `None` (usado pro campo de crédito, "não usar crédito")."""
    if raw is None or str(raw).strip() == "":
        return None
    raw = str(raw).strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValidationError({field: "Valor inválido."})
    if value < 0:
        raise ValidationError({field: "Valor inválido."})
    return value


def _comanda_groups(tenant):
    """Agrupa os atendimentos "em atendimento" por cliente — um cliente pode
    ter mais de um serviço na mesma comanda (ex.: fez o corte e decidiu fazer
    manicure na hora, ver `start_walk_in_service`), fechados juntos num
    pagamento só (`complete_client_comanda`). Os produtos adicionados
    (`ComandaProductItem`) são persistidos por CLIENTE — um só botão "Vender
    produto" por comanda, e sobrevivem a troca de aba/página até a comanda
    ser finalizada.

    SEM filtro de data (decisão do usuário em 2026-07-29): comanda aberta é
    comanda aberta — atendimento antecipado (iniciado num dia antes da data
    agendada) e comanda esquecida de dias anteriores precisam aparecer aqui,
    senão ficam "em andamento" pra sempre sem nenhuma UI pra fechar/cancelar.
    A data aparece no card quando não é hoje.

    Clientes com produto pendente mas SEM nenhum atendimento (ex.: removeu o
    único serviço da comanda por engano — `remove_appointment_from_comanda`
    — depois de já ter posto produto) também aparecem, senão o carrinho
    fica "preso" sem nenhuma UI pra ver/finalizar."""
    appointments = (
        Appointment.objects.for_tenant(tenant)
        .filter(status=AppointmentStatus.IN_PROGRESS)
        .select_related("client", "employee", "service", "package")
        # RF48 — pré-carrega a receita do serviço pra prévia informativa
        # "Insumos: ..." em _comandas.html, sem N+1.
        .prefetch_related("service__recipe_items__product")
        .order_by("client_id", "date", "start_time")
    )
    clients_by_id = {}
    appointments_by_client_id = {}
    order = []
    for client, rows in groupby(appointments, key=lambda a: a.client):
        clients_by_id[client.pk] = client
        appointments_by_client_id[client.pk] = list(rows)
        order.append(client.pk)

    pending_by_client_id = {}
    for item in ComandaProductItem.objects.for_tenant(tenant).select_related("client", "product"):
        pending_by_client_id.setdefault(item.client_id, []).append(item)
        if item.client_id not in clients_by_id:
            clients_by_id[item.client_id] = item.client
            order.append(item.client_id)

    groups = []
    for client_id in order:
        client = clients_by_id[client_id]
        rows = appointments_by_client_id.get(client_id, [])
        pending_items = [
            {
                "id": item.pk,
                "product": item.product,
                "quantity": item.quantity,
                "line_total": item.quantity * item.product.sale_price,
                "is_whole_unit": item.product.unit in WHOLE_UNIT_CODES,
            }
            for item in pending_by_client_id.get(client_id, [])
        ]
        products_total = sum((line["line_total"] for line in pending_items), Decimal("0"))
        groups.append(
            {
                "client": client,
                "appointments": rows,
                "pending_items": pending_items,
                # Serviço coberto por pacote de mensalidade não entra no total
                # a pagar — já foi pago quando o pacote foi atribuído ao
                # cliente (mesma regra de `complete_client_comanda`).
                "services_total": sum(
                    (a.price_at_booking for a in rows if a.package_id is None), Decimal("0")
                ),
                "products_total": products_total,
            }
        )
    return groups


def _period_context(request):
    today = datetime.date.today()
    start = _parse_date(request.GET.get("start"), today.replace(day=1))
    end = _parse_date(request.GET.get("end"), today)
    summary = finance_ops.period_summary(request.tenant, start, end)
    return {
        **summary,
        "start": start,
        "end": end,
        "today": today,
        "commissions_by_employee": _commissions_by_employee(request.tenant, start, end),
        "comanda_groups": _comanda_groups(request.tenant),
        "all_products": Product.objects.for_tenant(request.tenant).filter(is_active=True),
        # Crédito do cliente deixou de ser uma opção do <select> — agora é um
        # campo de valor à parte ("abater ou não") — ver comanda_finalize(_group).
        "payment_methods": REAL_MONEY_METHODS,
    }


def _cash_response(request, prompt_preferences_for=None, stock_warnings=None):
    """`prompt_preferences_for`: cliente recém-atendido — quando informado,
    em vez de só limpar o modal, abre o prompt "quer atualizar as
    observações desse cliente?" (ver `comanda_finalize_group`).

    `stock_warnings` (RF48): mensagens de insumo que ficou negativo — vira
    um toast avulso (mesmo mecanismo `hx-swap-oob="beforeend:#toast-slot"`
    de `templates/painel/notifications/_toast_poll.html`), que NÃO se
    auto-fecha (precisa de ação do admin)."""
    context = _period_context(request)
    items = render_to_string("painel/finance/_items.html", context, request=request)
    if prompt_preferences_for is not None:
        modal = render_to_string(
            "painel/clients/_finalize_preferences_prompt.html",
            {"client": prompt_preferences_for},
            request=request,
        )
        modal_reset = f'<div id="modal-slot" hx-swap-oob="true">{modal}</div>'
    else:
        modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    toast = ""
    if stock_warnings:
        toast = render_to_string(
            "painel/finance/_stock_warning_toast.html",
            {"stock_warnings": stock_warnings},
            request=request,
        )
    return HttpResponse(items + modal_reset + toast)


@tenant_admin_required
def cash_list(request):
    context = _period_context(request)
    context["active_nav"] = "finance"
    return render(request, "painel/finance/list.html", context)


@tenant_admin_required
def expense_create(request):
    query = request.GET.urlencode()
    if request.method == "POST":
        form = ExpenseForm(request.POST, tenant=request.tenant)
        if form.is_valid():
            finance_ops.create_expense(
                tenant=request.tenant, created_by=request.user, **form.cleaned_data
            )
            return _cash_response(request)
    else:
        form = ExpenseForm(tenant=request.tenant)
    response = render(
        request, "painel/finance/_expense_form.html", {"form": form, "query": query}
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/caixa/categorias-despesa/
# ---------------------------------------------------------------------------


def _get_expense_category(request, pk):
    return get_object_or_404(ExpenseCategory.objects.for_tenant(request.tenant), pk=pk)


def _expense_category_items_response(request):
    items = render_to_string(
        "painel/finance/expense_categories/_items.html",
        {"categories": ExpenseCategory.objects.for_tenant(request.tenant)},
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset)


@tenant_admin_required
def expense_category_list(request):
    return render(
        request,
        "painel/finance/expense_categories/list.html",
        {
            "categories": ExpenseCategory.objects.for_tenant(request.tenant),
            "active_nav": "finance",
        },
    )


@tenant_admin_required
def expense_category_create(request):
    if request.method == "POST":
        form = ExpenseCategoryForm(request.POST)
        if form.is_valid():
            finance_ops.create_expense_category(tenant=request.tenant, **form.cleaned_data)
            return _expense_category_items_response(request)
    else:
        form = ExpenseCategoryForm()
    response = render(
        request,
        "painel/finance/expense_categories/_form.html",
        {"form": form, "title": "Nova Categoria de Despesa"},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def expense_category_update(request, pk):
    category = _get_expense_category(request, pk)
    if request.method == "POST":
        form = ExpenseCategoryForm(request.POST, instance=category)
        if form.is_valid():
            finance_ops.update_expense_category(category, **form.cleaned_data)
            return _expense_category_items_response(request)
    else:
        form = ExpenseCategoryForm(instance=category)
    response = render(
        request,
        "painel/finance/expense_categories/_form.html",
        {"form": form, "title": "Editar Categoria de Despesa", "category": category},
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


@tenant_admin_required
def expense_category_toggle_confirm(request, pk):
    category = _get_expense_category(request, pk)
    if category.is_active:
        context = {
            "title": "Desativar categoria",
            "message": (
                f"Desativar '{category.name}'? Ela deixa de aparecer pra escolher em "
                f"novas despesas — lançamentos já feitos com ela não são afetados."
            ),
            "icon": "toggle_off",
            "confirm_label": "Desativar",
        }
    else:
        context = {
            "title": "Reativar categoria",
            "message": f"Reativar '{category.name}'? Ela volta a aparecer pra escolher em novas despesas.",
            "icon": "toggle_on",
            "confirm_label": "Reativar",
        }
    context.update({
        "action_url": reverse("finance:expense_category_toggle", args=[category.pk]),
        "target": "#expense-category-items",
    })
    return render(request, "painel/_confirm_action.html", context)


@tenant_admin_required
def expense_category_toggle(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    category = _get_expense_category(request, pk)
    finance_ops.set_expense_category_active(category, not category.is_active)
    return _expense_category_items_response(request)


@tenant_admin_required
def commission_pay_confirm(request, pk):
    commission = get_object_or_404(Commission.objects.for_tenant(request.tenant), pk=pk)
    query = request.GET.urlencode()
    return render(
        request,
        "painel/finance/_pay_commission_form.html",
        {
            "commission": commission,
            "form": PayCommissionForm(),
            "action_url": f"{reverse('finance:commission_pay', args=[commission.pk])}?{query}",
        },
    )


@tenant_admin_required
def commission_pay(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    commission = get_object_or_404(Commission.objects.for_tenant(request.tenant), pk=pk)
    form = PayCommissionForm(request.POST)
    if form.is_valid():
        try:
            finance_ops.mark_commission_paid(
                commission,
                payment_method=form.cleaned_data["payment_method"],
                created_by=request.user,
            )
        except ValidationError:
            return HttpResponse(status=409)
        return _cash_response(request)
    return HttpResponse(status=400)


def _grouped_products(tenant):
    products = (
        Product.objects.for_tenant(tenant)
        .filter(is_active=True, is_for_sale=True)
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


@tenant_admin_required
def product_picker(request, client_id):
    """Modal de venda de produto: lista os produtos ativos agrupados por
    categoria (com busca), pra adicionar à comanda em andamento do cliente —
    um botão só por comanda (não mais por serviço/atendimento)."""
    client = get_object_or_404(Client.objects.for_tenant(request.tenant), pk=client_id)
    categories_with_products, uncategorized_products = _grouped_products(request.tenant)
    return render(
        request,
        "painel/finance/_product_picker_modal.html",
        {
            "client": client,
            "categories_with_products": categories_with_products,
            "uncategorized_products": uncategorized_products,
        },
    )


@tenant_admin_required
def comanda_item_add(request, client_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    client = get_object_or_404(Client.objects.for_tenant(request.tenant), pk=client_id)
    product = get_object_or_404(
        Product.objects.for_tenant(request.tenant),
        pk=request.POST.get("product_id"), is_active=True, is_for_sale=True,
    )
    finance_ops.add_comanda_product_item(client=client, product=product, created_by=request.user)
    return _cash_response(request)


@tenant_admin_required
def comanda_item_update(request, item_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    item = get_object_or_404(ComandaProductItem.objects.for_tenant(request.tenant), pk=item_id)
    try:
        quantity = _parse_decimal(request.POST.get("quantity"), field="quantity") or Decimal("0")
        finance_ops.set_comanda_product_item_quantity(item, quantity)
    except ValidationError:
        return HttpResponse(status=409)
    return _cash_response(request)


@tenant_admin_required
def comanda_item_remove(request, item_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    item = get_object_or_404(ComandaProductItem.objects.for_tenant(request.tenant), pk=item_id)
    finance_ops.remove_comanda_product_item(item)
    return _cash_response(request)


@tenant_admin_required
def comanda_finalize_group(request):
    """Finaliza de uma vez todos os atendimentos "Em Atendimento" de um MESMO
    cliente (ex.: corte + manicure feitos na mesma visita) — um pagamento só
    pra tudo, cada atendimento gera sua própria comissão/estoque normalmente
    (`complete_client_comanda`). Os produtos vêm dos itens pendentes
    (`ComandaProductItem`) já persistidos — não do POST — e são apagados
    (viraram venda de verdade) só depois do sucesso.

    Se todos os serviços da comanda foram removidos (`remove_appointment_from_comanda`)
    mas ainda sobraram produtos pendentes, vira uma venda avulsa direta
    (`sell_products`) — sem comissão, já que não tem serviço nenhum envolvido."""
    if request.method != "POST":
        return HttpResponse(status=405)
    client = get_object_or_404(Client.objects.for_tenant(request.tenant), pk=request.POST.get("client_id"))
    appointment_ids = request.POST.getlist("appointment_id")
    appointments = list(
        Appointment.objects.for_tenant(request.tenant).filter(
            pk__in=appointment_ids, status=AppointmentStatus.IN_PROGRESS
        )
    )
    pending_items = list(finance_ops.comanda_product_items_for_client(client))
    if not appointments and not pending_items:
        return HttpResponse(status=409)

    payment_method = request.POST.get("payment_method")
    product_usage = [
        {"product": item.product, "quantity": item.quantity, "unit_price": item.product.sale_price}
        for item in pending_items
    ]

    stock_warnings = []
    try:
        if appointments:
            # Produtos não pertencem a um serviço específico (um botão só pra
            # comanda toda) — atribuídos ao primeiro atendimento da lista só
            # pra rastro (related_appointment), sem efeito no total/comissão.
            complete_client_comanda(
                appointments=appointments,
                payment_method=payment_method,
                created_by=request.user,
                product_usage_by_appointment={appointments[0].pk: product_usage},
                credit_amount=_parse_decimal(request.POST.get("credit_amount"), field="credit_amount"),
                debt_amount=_parse_decimal(request.POST.get("debt_amount"), field="debt_amount"),
                collect_prior_debt_amount=_parse_decimal(
                    request.POST.get("collect_prior_debt_amount"), field="collect_prior_debt_amount"
                ),
                stock_warnings=stock_warnings,
            )
        else:
            finance_ops.sell_products(
                tenant=request.tenant,
                product_usage=product_usage,
                payment_method=payment_method,
                created_by=request.user,
            )
    except ValidationError:
        return HttpResponse(status=409)

    for item in pending_items:
        item.delete()
    # Só pergunta sobre observações quando houve atendimento de verdade — uma
    # venda avulsa de produto (sem serviço) não tem esse contexto de CRM.
    return _cash_response(
        request,
        prompt_preferences_for=client if appointments else None,
        stock_warnings=stock_warnings,
    )


@tenant_admin_required
def comanda_service_remove_confirm(request, pk):
    appointment = get_object_or_404(
        Appointment.objects.for_tenant(request.tenant).select_related("client", "service", "employee"),
        pk=pk,
        status=AppointmentStatus.IN_PROGRESS,
    )
    return render(request, "painel/finance/_confirm_remove_service.html", {"appointment": appointment})


@tenant_admin_required
def comanda_service_remove(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    appointment = get_object_or_404(
        Appointment.objects.for_tenant(request.tenant), pk=pk, status=AppointmentStatus.IN_PROGRESS
    )
    try:
        remove_appointment_from_comanda(appointment)
    except ValidationError:
        return HttpResponse(status=409)
    return _cash_response(request)


@tenant_admin_required
def sale_picker(request):
    """Modal "Nova Venda": venda de produto avulsa, sem nenhum
    serviço/agendamento envolvido — cliente que só passou pra comprar algo."""
    categories_with_products, uncategorized_products = _grouped_products(request.tenant)
    return render(
        request,
        "painel/finance/_sale_picker_modal.html",
        {
            "categories_with_products": categories_with_products,
            "uncategorized_products": uncategorized_products,
            "payment_methods": REAL_MONEY_METHODS,
        },
    )


@tenant_admin_required
def sale_create(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        product_usage = build_product_usage(
            tenant=request.tenant,
            product_ids=request.POST.getlist("product_id"),
            quantities=request.POST.getlist("product_qty"),
        )
        finance_ops.sell_products(
            tenant=request.tenant,
            product_usage=product_usage,
            payment_method=request.POST.get("payment_method"),
            created_by=request.user,
        )
    except ValidationError:
        return HttpResponse(status=409)
    return _cash_response(request)


# ---------------------------------------------------------------------------
# Painel (HTMX) — adicionar serviço avulso na comanda (cliente já no salão)
# ---------------------------------------------------------------------------


@tenant_admin_required
def walk_in_service_picker(request, client_id):
    """Modal passo 1: busca/lista os serviços ativos e "vendáveis" (mesma
    regra de `bookable_services` usada na página pública — precisa ter ao
    menos 1 profissional ativo vinculado)."""
    client = get_object_or_404(Client.objects.for_tenant(request.tenant), pk=client_id)
    services = bookable_services(request.tenant)
    return render(
        request,
        "painel/finance/_walk_in_service_picker.html",
        {"client": client, "services": services},
    )


@tenant_admin_required
def walk_in_employee_list(request, client_id, service_id):
    """Modal passo 2 (swap parcial dentro do mesmo modal): lista os
    profissionais ativos vinculados ao serviço escolhido."""
    client = get_object_or_404(Client.objects.for_tenant(request.tenant), pk=client_id)
    service = get_object_or_404(
        Service.objects.for_tenant(request.tenant), pk=service_id, is_active=True
    )
    employees = Employee.objects.for_tenant(request.tenant).filter(
        is_active=True, service_links__service=service
    )
    return render(
        request,
        "painel/finance/_walk_in_employee_list.html",
        {"client": client, "service": service, "employees": employees},
    )


@tenant_admin_required
def walk_in_service_add(request, client_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    client = get_object_or_404(Client.objects.for_tenant(request.tenant), pk=client_id)
    service = get_object_or_404(
        Service.objects.for_tenant(request.tenant),
        pk=request.POST.get("service_id"),
        is_active=True,
    )
    employee = get_object_or_404(
        Employee.objects.for_tenant(request.tenant),
        pk=request.POST.get("employee_id"),
        is_active=True,
    )
    try:
        start_walk_in_service(
            tenant=request.tenant,
            client=client,
            employee=employee,
            service=service,
            created_by=request.user,
        )
    except ValidationError:
        return HttpResponse(status=409)
    return _cash_response(request)


@tenant_admin_required
def commission_pay_all_confirm(request, employee_pk):
    employee = get_object_or_404(Employee.objects.for_tenant(request.tenant), pk=employee_pk)
    query = request.GET.urlencode()
    start = _parse_date(request.GET.get("start"), datetime.date.today().replace(day=1))
    end = _parse_date(request.GET.get("end"), datetime.date.today())
    pending_total = Commission.objects.for_tenant(request.tenant).filter(
        employee=employee, status=CommissionStatus.PENDING, created_at__date__range=(start, end),
    ).aggregate(total=Sum("calculated_amount"))["total"] or Decimal("0")
    return render(
        request,
        "painel/finance/_pay_all_commissions_form.html",
        {
            "employee": employee,
            "pending_total": pending_total,
            "form": PayCommissionForm(),
            "action_url": f"{reverse('finance:commission_pay_all', args=[employee.pk])}?{query}",
        },
    )


@tenant_admin_required
def commission_pay_all(request, employee_pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    employee = get_object_or_404(Employee.objects.for_tenant(request.tenant), pk=employee_pk)
    start = _parse_date(request.GET.get("start"), datetime.date.today().replace(day=1))
    end = _parse_date(request.GET.get("end"), datetime.date.today())
    form = PayCommissionForm(request.POST)
    if form.is_valid():
        try:
            finance_ops.pay_all_commissions_for_employee(
                request.tenant, employee,
                start_date=start, end_date=end,
                payment_method=form.cleaned_data["payment_method"],
                created_by=request.user,
            )
        except ValidationError:
            return HttpResponse(status=409)
        return _cash_response(request)
    return HttpResponse(status=400)


@employee_required
def my_commissions(request):
    """RF12 — só as próprias comissões do funcionário logado, filtráveis por
    período. Nunca expõe o caixa geral do tenant nem comissões de outros
    funcionários."""
    employee = request.user.employee_profile
    today = datetime.date.today()
    start = _parse_date(request.GET.get("start"), today.replace(day=1))
    end = _parse_date(request.GET.get("end"), today)

    commissions = (
        Commission.objects.for_tenant(request.tenant)
        .filter(employee=employee, created_at__date__range=(start, end))
        .select_related("appointment__service")
        .order_by("-created_at")
    )
    pending_total = commissions.filter(status=CommissionStatus.PENDING).aggregate(
        total=Sum("calculated_amount")
    )["total"] or Decimal("0")
    paid_total = commissions.filter(status=CommissionStatus.PAID).aggregate(
        total=Sum("calculated_amount")
    )["total"] or Decimal("0")

    return render(
        request,
        "painel/finance/my_commissions.html",
        {
            "commissions": commissions,
            "start": start,
            "end": end,
            "pending_total": pending_total,
            "paid_total": paid_total,
            "active_nav": "my_commissions",
        },
    )
