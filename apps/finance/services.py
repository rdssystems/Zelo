"""Operações de domínio financeiro (RF16, RF21-RF24).

`Commission.calculated_amount` e os valores de `CashTransaction` são sempre
SNAPSHOT no momento da criação — nunca recalculados depois com dado que
pode ter mudado (CLAUDE.md regra 4).
"""

from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.employees.models import CommissionType
from apps.employees.services import get_commission_config
from apps.inventory.models import WHOLE_UNIT_CODES, MovementReason, MovementType
from apps.inventory.services import register_stock_movement

from .models import (
    CashCategory,
    CashFlowType,
    CashTransaction,
    ComandaProductItem,
    Commission,
    CommissionStatus,
    ExpenseCategory,
    PaymentMethod,
)


def _validate_cash_transaction(type_, category, amount, payment_method):
    if type_ not in CashFlowType.values:
        raise ValidationError("Tipo de lançamento inválido.")
    if category not in CashCategory.values:
        raise ValidationError("Categoria de lançamento inválida.")
    if payment_method not in PaymentMethod.values:
        raise ValidationError("Forma de pagamento inválida.")
    if payment_method == PaymentMethod.CLIENT_CREDIT:
        # Pagamento com crédito do cliente NUNCA vira lançamento de caixa —
        # a receita já entrou na recarga (CLIENT_CREDIT_TOPUP). Aceitar aqui
        # contaria o mesmo dinheiro duas vezes no saldo do período.
        raise ValidationError(
            "Crédito do cliente não gera lançamento de caixa — o dinheiro já entrou na recarga."
        )
    if Decimal(amount) <= 0:
        raise ValidationError({"amount": "O valor deve ser maior que zero."})


def create_cash_transaction(
    *,
    tenant,
    type,
    category,
    amount,
    payment_method,
    created_by,
    description="",
    related_appointment=None,
    related_stock_movement=None,
    related_commission=None,
    expense_category=None,
):
    _validate_cash_transaction(type, category, amount, payment_method)
    if expense_category is not None and expense_category.tenant_id != tenant.id:
        raise ValidationError("Categoria de despesa não pertence a este tenant.")
    return CashTransaction.objects.create(
        tenant=tenant,
        type=type,
        category=category,
        amount=amount,
        payment_method=payment_method,
        description=description or "",
        related_appointment=related_appointment,
        related_stock_movement=related_stock_movement,
        related_commission=related_commission,
        expense_category=expense_category,
        created_by=created_by,
    )


def create_expense(*, tenant, amount, payment_method, description, created_by, expense_category=None):
    """RF23: despesa avulsa (aluguel, contas, etc.) lançada manualmente.

    `expense_category` é opcional (decisão do usuário em 2026-08-06) — sem
    ela, a despesa continua valendo pro saldo do Caixa, só não entra na
    quebra fixo/variável do DRE (fica agrupada como "sem categoria")."""
    return create_cash_transaction(
        tenant=tenant,
        type=CashFlowType.OUT,
        category=CashCategory.EXPENSE,
        amount=amount,
        payment_method=payment_method,
        description=description,
        created_by=created_by,
        expense_category=expense_category,
    )


# ---------------------------------------------------------------------------
# Categoria de despesa (RF novo, 2026-08-06) — separa comissão (custo direto
# do serviço) de despesa administrativa, e classifica esta em fixa/variável.
# ---------------------------------------------------------------------------


def _validate_expense_category(name):
    if not str(name).strip():
        raise ValidationError({"name": "O nome da categoria é obrigatório."})


def create_expense_category(*, tenant, name, is_fixed=True):
    _validate_expense_category(name)
    return ExpenseCategory.objects.create(tenant=tenant, name=str(name).strip(), is_fixed=is_fixed)


def update_expense_category(category, *, name, is_fixed):
    _validate_expense_category(name)
    category.name = str(name).strip()
    category.is_fixed = is_fixed
    category.save(update_fields=["name", "is_fixed"])
    return category


def set_expense_category_active(category, is_active):
    category.is_active = bool(is_active)
    category.save(update_fields=["is_active"])
    return category


def create_commission_for_appointment(appointment):
    """RF16: snapshot da regra de prioridade EmployeeService > Employee
    (apps/employees/services.py::get_commission_config), calculado sobre o
    preço travado no momento do agendamento (`price_at_booking`)."""
    commission_type, commission_value = get_commission_config(
        appointment.employee, appointment.service
    )
    base_amount = appointment.price_at_booking
    if commission_type == CommissionType.PERCENTAGE:
        calculated_amount = (base_amount * commission_value / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    else:
        calculated_amount = commission_value

    return Commission.objects.create(
        tenant=appointment.tenant,
        employee=appointment.employee,
        appointment=appointment,
        commission_type=commission_type,
        commission_value=commission_value,
        base_amount=base_amount,
        calculated_amount=calculated_amount,
        status=CommissionStatus.PENDING,
    )


def mark_commission_paid(commission, *, payment_method, created_by):
    """RF24: marca comissão como paga e gera a saída de caixa vinculada."""
    if commission.status != CommissionStatus.PENDING:
        raise ValidationError("Esta comissão já foi paga.")

    create_cash_transaction(
        tenant=commission.tenant,
        type=CashFlowType.OUT,
        category=CashCategory.COMMISSION_PAYMENT,
        amount=commission.calculated_amount,
        payment_method=payment_method,
        description=f"Comissão — {commission.employee.full_name}",
        created_by=created_by,
        related_commission=commission,
    )
    commission.status = CommissionStatus.PAID
    commission.paid_at = timezone.now()
    commission.save(update_fields=["status", "paid_at"])
    return commission


@transaction.atomic
def pay_all_commissions_for_employee(tenant, employee, *, start_date, end_date, payment_method, created_by):
    """Paga de uma vez todas as comissões PENDENTES do funcionário dentro do
    período filtrado no Caixa — mantém uma `CashTransaction` por comissão
    (rastro individual intacto, ver `mark_commission_paid`), só evita ter
    que clicar "pagar" comissão por comissão quando o dono do salão acerta
    tudo de uma vez (semanal/mensal)."""
    pending = Commission.objects.for_tenant(tenant).filter(
        employee=employee,
        status=CommissionStatus.PENDING,
        created_at__date__range=(start_date, end_date),
    )
    if not pending.exists():
        raise ValidationError("Não há comissões pendentes para pagar neste período.")
    for commission in pending:
        mark_commission_paid(commission, payment_method=payment_method, created_by=created_by)


def period_summary(tenant, start_date, end_date):
    """RF22: saldo do período + totais por categoria, para o painel de caixa."""
    qs = CashTransaction.objects.for_tenant(tenant).filter(
        created_at__date__range=(start_date, end_date)
    )
    total_in = qs.filter(type=CashFlowType.IN).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    total_out = qs.filter(type=CashFlowType.OUT).aggregate(total=Sum("amount"))["total"] or Decimal("0")

    # Card "por tipo de venda" do Caixa (pedido do usuário em 2026-08-09,
    # pro layout 2x2 no mobile) — sempre essas 4 categorias fixas, mesmo com
    # total zero (senão o card "pula" de tamanho conforme o período tem ou
    # não movimento de cada tipo).
    sale_type_categories = (
        CashCategory.SERVICE_SALE,
        CashCategory.PRODUCT_SALE,
        CashCategory.COMMISSION_PAYMENT,
        CashCategory.PACKAGE_SALE,
    )
    sale_type_breakdown = [
        {
            "label": category.label,
            "total": qs.filter(category=category).aggregate(total=Sum("amount"))["total"]
            or Decimal("0"),
        }
        for category in sale_type_categories
    ]

    # `by_category` não repete o que já está no card "por tipo de venda"
    # acima (pedido do usuário em 2026-08-09) — só mostra categoria que não
    # tem card dedicado (recarga de crédito, cobrança de débito, despesa,
    # outro).
    by_category = []
    for value, label in CashCategory.choices:
        if value in sale_type_categories:
            continue
        category_total = qs.filter(category=value).aggregate(total=Sum("amount"))["total"]
        if category_total:
            by_category.append({"category": label, "total": category_total})

    return {
        "total_in": total_in,
        "total_out": total_out,
        "balance": total_in - total_out,
        "by_category": by_category,
        "sale_type_breakdown": sale_type_breakdown,
        "transactions": qs.select_related(
            "related_appointment__client", "related_appointment__service"
        ),
    }


def _expense_rows_by_fixed(expense_qs, *, is_fixed):
    rows = (
        expense_qs.filter(expense_category__is_fixed=is_fixed)
        .values("expense_category__name")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )
    return [{"name": row["expense_category__name"], "total": row["total"]} for row in rows]


def dre_breakdown(tenant, start_date, end_date):
    """DRE simplificado em cascata (decisão do usuário em 2026-08-06):
    Receita → Custo Direto (comissão) → Margem de Contribuição → Despesas
    Fixas/Variáveis → Resultado.

    Função separada de `period_summary` de propósito — aquela alimenta o
    Caixa (RF22) e não pode mudar de formato; esta é só pro DRE (aba
    Relatórios + PDF). Comissão paga é tratada como custo DIRETO do serviço
    (não despesa administrativa — é o que o usuário pediu pra separar).
    Despesa (`CashCategory.EXPENSE`) é quebrada em fixa/variável pela
    `ExpenseCategory` do lançamento; sem categoria, cai em "sem categoria" —
    continua contando pro resultado, só não teve o custo classificado.
    """
    qs = CashTransaction.objects.for_tenant(tenant).filter(
        created_at__date__range=(start_date, end_date)
    )

    def _sum(filtered_qs):
        return filtered_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0")

    revenue = _sum(qs.filter(type=CashFlowType.IN))
    direct_cost = _sum(qs.filter(type=CashFlowType.OUT, category=CashCategory.COMMISSION_PAYMENT))
    contribution_margin = revenue - direct_cost

    expense_qs = qs.filter(type=CashFlowType.OUT, category=CashCategory.EXPENSE)
    fixed_total = _sum(expense_qs.filter(expense_category__is_fixed=True))
    variable_total = _sum(expense_qs.filter(expense_category__is_fixed=False))
    uncategorized_total = _sum(expense_qs.filter(expense_category__isnull=True))

    # Saída que não é comissão nem despesa avulsa (hoje só CashCategory.OTHER,
    # categoria reservada) — soma à parte pra "Resultado" nunca destoar do
    # saldo real de caixa do período.
    other_out = _sum(
        qs.filter(type=CashFlowType.OUT).exclude(
            category__in=[CashCategory.COMMISSION_PAYMENT, CashCategory.EXPENSE]
        )
    )

    total_expenses = fixed_total + variable_total + uncategorized_total + other_out
    result = contribution_margin - total_expenses

    return {
        "revenue": revenue,
        "direct_cost": direct_cost,
        "contribution_margin": contribution_margin,
        "fixed_total": fixed_total,
        "fixed_by_category": _expense_rows_by_fixed(expense_qs, is_fixed=True),
        "variable_total": variable_total,
        "variable_by_category": _expense_rows_by_fixed(expense_qs, is_fixed=False),
        "uncategorized_total": uncategorized_total,
        "other_out": other_out,
        "total_expenses": total_expenses,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Comanda — carrinho de produto persistente (RF: sobreviver a troca de aba/
# página antes de finalizar; um botão só por comanda, não por serviço)
# ---------------------------------------------------------------------------


def _validate_item_quantity(product, quantity):
    if quantity <= 0:
        raise ValidationError({"quantity": "A quantidade deve ser maior que zero."})
    if product.unit in WHOLE_UNIT_CODES and quantity != quantity.to_integral_value():
        raise ValidationError(
            {
                "quantity": (
                    f"Quantidade de \"{product.name}\" deve ser um número inteiro "
                    f"({product.get_unit_display()} não aceita fração)."
                )
            }
        )


def add_comanda_product_item(*, client, product, created_by):
    """Adiciona um produto à comanda em andamento do cliente — fica pendente
    (sem mexer em estoque/caixa ainda) até a comanda ser finalizada. Clicar
    de novo no mesmo produto não duplica a linha (mesmo comportamento do
    carrinho antigo em Alpine, agora persistido no banco)."""
    if product.tenant_id != client.tenant_id:
        raise ValidationError("Produto não pertence a este tenant.")
    item, _created = ComandaProductItem.objects.get_or_create(
        tenant=client.tenant,
        client=client,
        product=product,
        defaults={"quantity": Decimal("1"), "created_by": created_by},
    )
    return item


def set_comanda_product_item_quantity(item, quantity):
    _validate_item_quantity(item.product, quantity)
    item.quantity = quantity
    item.save(update_fields=["quantity"])
    return item


def remove_comanda_product_item(item):
    item.delete()


def comanda_product_items_for_client(client):
    return (
        ComandaProductItem.objects.for_tenant(client.tenant)
        .filter(client=client)
        .select_related("product")
    )


# ---------------------------------------------------------------------------
# Venda avulsa de produto (sem serviço/agendamento envolvido — cliente entra
# só pra comprar algo)
# ---------------------------------------------------------------------------


@transaction.atomic
def sell_products(*, tenant, product_usage, payment_method, created_by):
    """Venda de produto sem nenhum serviço/agendamento — `StockMovement` +
    `CashTransaction` direto, sem `Commission` (comissão é só de serviço).

    `product_usage`: lista de dicts `{"product": Product, "quantity": Decimal,
    "unit_price": Decimal}` (mesmo formato usado na conclusão de comanda).
    """
    if not product_usage:
        raise ValidationError("Nenhum produto selecionado para a venda.")

    total = Decimal("0")
    for item in product_usage:
        product = item["product"]
        quantity = item["quantity"]
        unit_price = item["unit_price"]
        movement = register_stock_movement(
            tenant=tenant,
            product=product,
            movement_type=MovementType.OUT,
            quantity=quantity,
            unit_price=unit_price,
            reason=MovementReason.SALE,
            created_by=created_by,
        )
        item_total = quantity * unit_price
        total += item_total
        create_cash_transaction(
            tenant=tenant,
            type=CashFlowType.IN,
            category=CashCategory.PRODUCT_SALE,
            amount=item_total,
            payment_method=payment_method,
            description=f"Venda avulsa: {product.name}",
            created_by=created_by,
            related_stock_movement=movement,
        )
    return total
