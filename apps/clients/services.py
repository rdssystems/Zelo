"""Operações de domínio de cliente final (RF04, RNF07) e do CRM.

Cliente nunca tem senha (CLAUDE.md, regra 6) — telefone é o identificador
único dentro do tenant.

CRM: `Client.credit_balance` é derivado — NUNCA editar direto (regra 2 do
CLAUDE.md, mesma regra de `Product.current_stock`). Toda mudança passa por
`add_client_credit`/`remove_client_credit`/`redeem_client_credit_for_appointment`,
que registram um `ClientCreditTransaction` com o motivo.
"""

import calendar
import datetime
import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Client, ClientCreditTransaction


def normalize_phone(raw_phone):
    """Normaliza para um formato canônico (só dígitos, sem DDI) e valida
    formato BR: DDD (2 dígitos) + número (8 ou 9 dígitos) = 10 ou 11 dígitos.
    Aceita o telefone digitado com ou sem +55/55 na frente.
    """
    digits = "".join(re.findall(r"\d", raw_phone or ""))
    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]
    if len(digits) not in (10, 11):
        raise ValidationError(
            "Telefone inválido. Informe DDD + número, ex: (11) 91234-5678."
        )
    return digits


def format_phone_display(phone):
    """Só para exibição — o valor persistido em `Client.phone` continua puro."""
    if len(phone) == 11:
        return f"({phone[:2]}) {phone[2:7]}-{phone[7:]}"
    if len(phone) == 10:
        return f"({phone[:2]}) {phone[2:6]}-{phone[6:]}"
    return phone


def get_or_create_client(*, tenant, phone, name=""):
    """RF04: se o telefone já existe no tenant, recupera nome/histórico
    (ignora o nome informado agora); se não existe, cadastra na hora — exige
    nome nesse caso.

    Retorna `(client, created)`.
    """
    phone = normalize_phone(phone)
    try:
        return Client.objects.get(tenant=tenant, phone=phone), False
    except Client.DoesNotExist:
        name = (name or "").strip()
        if not name:
            raise ValidationError(
                {"name": "Informe seu nome para o primeiro agendamento."}
            )
        return Client.objects.create(tenant=tenant, phone=phone, name=name), True


@transaction.atomic
def anonymize_client(client):
    """RNF07 (LGPD): o cliente final pode pedir a exclusão dos seus dados.

    `Appointment.client` é `PROTECT` (nunca perder o histórico financeiro/de
    auditoria — regra usada em todo o projeto) — então não dá pra apagar a
    linha do `Client`. Em vez disso, anonimizamos o nome/telefone e cancelamos
    os agendamentos futuros (pendente/confirmado); o histórico já concluído
    permanece, só sem o dado pessoal.
    """
    from apps.scheduling.models import Appointment, BLOCKING_STATUSES
    from apps.scheduling.services import cancel_appointment

    for appointment in Appointment.objects.filter(
        client=client, status__in=BLOCKING_STATUSES
    ):
        cancel_appointment(appointment)

    client.name = "Cliente removido (LGPD)"
    client.phone = f"removido-{client.pk}"
    # Política LGPD deliberada: só nome/telefone são dado pessoal. O saldo de
    # crédito e o ledger (passivo financeiro do salão), o status de mensalista
    # e o histórico de atendimentos permanecem — não identificam ninguém.
    # `preferences` pode conter observação pessoal, então também é apagado.
    client.preferences = ""
    client.save(update_fields=["name", "phone", "preferences"])
    return client


# ---------------------------------------------------------------------------
# CRM — dados cadastrais, mensalista e carteira de crédito
# ---------------------------------------------------------------------------


def update_client(client, *, name, phone, preferences=""):
    name = str(name).strip()
    if not name:
        raise ValidationError({"name": "O nome é obrigatório."})
    phone = normalize_phone(phone)
    conflict = Client.objects.filter(tenant=client.tenant, phone=phone).exclude(
        pk=client.pk
    )
    if conflict.exists():
        raise ValidationError(
            {"phone": "Já existe outro cliente com este telefone."}
        )
    client.name = name
    client.phone = phone
    client.preferences = (preferences or "").strip()
    client.save(update_fields=["name", "phone", "preferences"])
    return client


def create_client(*, tenant, name, phone, preferences=""):
    """Cadastro manual pelo painel (a página pública usa `get_or_create_client`)."""
    name = str(name).strip()
    if not name:
        raise ValidationError({"name": "O nome é obrigatório."})
    phone = normalize_phone(phone)
    if Client.objects.filter(tenant=tenant, phone=phone).exists():
        raise ValidationError({"phone": "Já existe um cliente com este telefone."})
    return Client.objects.create(
        tenant=tenant, name=name, phone=phone, preferences=(preferences or "").strip()
    )


def _add_months(d, months):
    """Soma meses ajustando o dia quando o mês de destino é mais curto
    (31/jan + 1 mês → 28 ou 29/fev). Evita depender de python-dateutil."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def set_subscriber_status(client, *, is_subscriber, due_date=None):
    """Habilita/desabilita mensalista. Ao habilitar, a data de vencimento é
    obrigatória; ao desabilitar, a data é limpa."""
    if is_subscriber and due_date is None:
        raise ValidationError(
            {"subscription_due_date": "Informe a data de vencimento da mensalidade."}
        )
    client.is_subscriber = bool(is_subscriber)
    client.subscription_due_date = due_date if is_subscriber else None
    client.save(update_fields=["is_subscriber", "subscription_due_date"])
    return client


def renew_subscription(client):
    """Empurra o vencimento +1 mês. Se já venceu, renova a partir de hoje
    (não acumula atraso); se ainda está em dia, soma a partir do vencimento."""
    if not client.is_subscriber or client.subscription_due_date is None:
        raise ValidationError("Este cliente não é mensalista.")
    base = max(client.subscription_due_date, datetime.date.today())
    client.subscription_due_date = _add_months(base, 1)
    client.save(update_fields=["subscription_due_date"])
    return client


def _validate_credit_amount(amount):
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError({"amount": "O valor deve ser maior que zero."})
    return amount


@transaction.atomic
def add_client_credit(client, *, amount, payment_method, created_by, reason="Recarga de crédito"):
    """Recarga: o dinheiro entra DE VERDADE no caixa agora — cria a
    `CashTransaction` (IN, CLIENT_CREDIT_TOPUP), o lançamento no ledger da
    carteira e soma no saldo. O uso posterior desse crédito não gera nova
    entrada (ver `redeem_client_credit_for_appointment`)."""
    from apps.finance.models import CashCategory, CashFlowType, PaymentMethod
    from apps.finance.services import create_cash_transaction

    amount = _validate_credit_amount(amount)
    if payment_method == PaymentMethod.CLIENT_CREDIT:
        raise ValidationError(
            {"payment_method": "Recarga não pode ser paga com o próprio crédito."}
        )
    cash_txn = create_cash_transaction(
        tenant=client.tenant,
        type=CashFlowType.IN,
        category=CashCategory.CLIENT_CREDIT_TOPUP,
        amount=amount,
        payment_method=payment_method,
        description=f"Recarga de crédito — {client.name}",
        created_by=created_by,
    )
    entry = ClientCreditTransaction.objects.create(
        tenant=client.tenant,
        client=client,
        type=CashFlowType.IN,
        amount=amount,
        reason=reason,
        related_cash_transaction=cash_txn,
        created_by=created_by,
    )
    client.credit_balance = client.credit_balance + amount
    client.save(update_fields=["credit_balance"])
    return entry


@transaction.atomic
def remove_client_credit(client, *, amount, created_by, reason="Ajuste manual"):
    """Remoção manual (correção de erro, estorno em dinheiro ao cliente):
    abate o saldo com registro no ledger, SEM mexer no caixa — se o salão
    devolveu dinheiro físico, o dono lança a despesa avulsa correspondente."""
    from apps.finance.models import CashFlowType

    amount = _validate_credit_amount(amount)
    if amount > client.credit_balance:
        raise ValidationError(
            {"amount": f"Saldo insuficiente: cliente tem R$ {client.credit_balance}."}
        )
    entry = ClientCreditTransaction.objects.create(
        tenant=client.tenant,
        client=client,
        type=CashFlowType.OUT,
        amount=amount,
        reason=reason,
        created_by=created_by,
    )
    client.credit_balance = client.credit_balance - amount
    client.save(update_fields=["credit_balance"])
    return entry


@transaction.atomic
def redeem_client_credit_for_appointment(client, *, amount, appointment, created_by):
    """Pagamento de comanda com crédito — chamado por
    `apps/scheduling/services.py::complete_appointment` quando a forma de
    pagamento é CLIENT_CREDIT. Abate o saldo e registra no ledger vinculado
    ao atendimento. NÃO cria `CashTransaction`: a receita já foi reconhecida
    no caixa quando a recarga aconteceu — criar de novo aqui contaria o mesmo
    dinheiro duas vezes."""
    from apps.finance.models import CashFlowType

    amount = _validate_credit_amount(amount)
    if amount > client.credit_balance:
        raise ValidationError(
            f"Crédito insuficiente: {client.name} tem R$ {client.credit_balance} "
            f"e a comanda soma R$ {amount}."
        )
    entry = ClientCreditTransaction.objects.create(
        tenant=client.tenant,
        client=client,
        type=CashFlowType.OUT,
        amount=amount,
        reason="Uso em atendimento",
        related_appointment=appointment,
        created_by=created_by,
    )
    client.credit_balance = client.credit_balance - amount
    client.save(update_fields=["credit_balance"])
    return entry
