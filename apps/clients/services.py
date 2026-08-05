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


def _validate_birthday(birth_day, birth_month):
    """Dia/mês de nascimento (sem ano — RF: só pra gerar alerta de
    aniversário, não guarda idade). Os dois juntos ou nenhum; dia validado
    contra o mês (usa 2024, ano bissexto, pra aceitar 29/02)."""
    has_day = birth_day not in (None, "")
    has_month = birth_month not in (None, "")
    if not has_day and not has_month:
        return None, None
    if has_day != has_month:
        raise ValidationError(
            {"birth_day": "Informe dia e mês de nascimento juntos, ou deixe os dois em branco."}
        )
    birth_day, birth_month = int(birth_day), int(birth_month)
    if not (1 <= birth_month <= 12):
        raise ValidationError({"birth_month": "Mês de nascimento inválido."})
    max_day = calendar.monthrange(2024, birth_month)[1]
    if not (1 <= birth_day <= max_day):
        raise ValidationError({"birth_day": "Dia de nascimento inválido para o mês informado."})
    return birth_day, birth_month


def get_or_create_client(*, tenant, phone, name="", birth_day=None, birth_month=None):
    """RF04: se o telefone já existe no tenant, recupera nome/histórico
    (ignora o nome/aniversário informado agora); se não existe, cadastra na
    hora — exige nome nesse caso, aniversário é opcional.

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
        birth_day, birth_month = _validate_birthday(birth_day, birth_month)
        return (
            Client.objects.create(
                tenant=tenant, phone=phone, name=name,
                birth_day=birth_day, birth_month=birth_month,
            ),
            True,
        )


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
    # `preferences` e data de nascimento podem conter observação/dado
    # pessoal, então também são apagados.
    client.preferences = ""
    client.birth_day = None
    client.birth_month = None
    client.save(update_fields=["name", "phone", "preferences", "birth_day", "birth_month"])
    return client


# ---------------------------------------------------------------------------
# CRM — dados cadastrais, mensalista e carteira de crédito
# ---------------------------------------------------------------------------


def update_client(client, *, name, phone, preferences="", birth_day=None, birth_month=None):
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
    birth_day, birth_month = _validate_birthday(birth_day, birth_month)
    client.name = name
    client.phone = phone
    client.preferences = (preferences or "").strip()
    client.birth_day = birth_day
    client.birth_month = birth_month
    client.save(update_fields=["name", "phone", "preferences", "birth_day", "birth_month"])
    return client


def update_client_preferences(client, preferences):
    """Atualiza só o texto de preferências/observações — usado no atalho de
    editar rápido (modal aberto na Agenda/Caixa), sem re-validar nome/telefone
    (que não mudam nesse fluxo)."""
    client.preferences = (preferences or "").strip()
    client.save(update_fields=["preferences"])
    return client


def create_client(*, tenant, name, phone, preferences="", birth_day=None, birth_month=None):
    """Cadastro manual pelo painel (a página pública usa `get_or_create_client`)."""
    name = str(name).strip()
    if not name:
        raise ValidationError({"name": "O nome é obrigatório."})
    phone = normalize_phone(phone)
    if Client.objects.filter(tenant=tenant, phone=phone).exists():
        raise ValidationError({"phone": "Já existe um cliente com este telefone."})
    birth_day, birth_month = _validate_birthday(birth_day, birth_month)
    return Client.objects.create(
        tenant=tenant, name=name, phone=phone, preferences=(preferences or "").strip(),
        birth_day=birth_day, birth_month=birth_month,
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
    obrigatória; ao desabilitar, a data é limpa — e o pacote (se algum
    estiver atribuído) também é desvinculado, já que só faz sentido
    enquanto o cliente é mensalista (decisão do usuário em 2026-08-04)."""
    if is_subscriber and due_date is None:
        raise ValidationError(
            {"subscription_due_date": "Informe a data de vencimento da mensalidade."}
        )
    client.is_subscriber = bool(is_subscriber)
    client.subscription_due_date = due_date if is_subscriber else None
    update_fields = ["is_subscriber", "subscription_due_date"]
    if not is_subscriber and client.package_id is not None:
        client.package = None
        update_fields.append("package")
    client.save(update_fields=update_fields)
    return client


@transaction.atomic
def renew_subscription(client, *, payment_method=None, created_by=None):
    """Empurra o vencimento +1 mês. Se já venceu, renova a partir de hoje
    (não acumula atraso); se ainda está em dia, soma a partir do vencimento.

    Cliente com pacote ativo (decisão do usuário em 2026-08-04): renovar
    também gera uma NOVA cobrança no Caixa (mesmo valor do pacote) — sem
    isso, a data avançava sem nenhum registro de que o mês foi pago de
    novo. `payment_method`/`created_by` são obrigatórios nesse caso, iguais
    à atribuição inicial (`assign_package_to_client`). Sem pacote, o
    comportamento continua o de sempre: só controle de data, pressupõe
    pagamento recebido por fora do sistema."""
    if not client.is_subscriber or client.subscription_due_date is None:
        raise ValidationError("Este cliente não é mensalista.")

    if client.package_id is not None:
        if not payment_method:
            raise ValidationError(
                {"payment_method": "Informe a forma de pagamento da renovação."}
            )
        from apps.finance.models import CashCategory, CashFlowType
        from apps.finance.services import create_cash_transaction

        create_cash_transaction(
            tenant=client.tenant,
            type=CashFlowType.IN,
            category=CashCategory.PACKAGE_SALE,
            amount=client.package.price,
            payment_method=payment_method,
            description=f"Renovação — Pacote {client.package.name} — {client.name}",
            created_by=created_by,
        )

    base = max(client.subscription_due_date, datetime.date.today())
    client.subscription_due_date = _add_months(base, 1)
    client.save(update_fields=["subscription_due_date"])
    return client


# ---------------------------------------------------------------------------
# Pacote de mensalidade (decisão do usuário em 2026-08-04) — camada em cima
# do mensalista tradicional acima: define quais serviços já estão inclusos
# na mensalidade do cliente, sem cobrança extra no Caixa (ver
# apps.scheduling.services.create_appointment/complete_appointment).
# ---------------------------------------------------------------------------


def create_package(*, tenant, name, description="", price, service_ids, generates_commission, created_by):
    from apps.services.models import Service

    from .models import Package

    name = str(name).strip()
    if not name:
        raise ValidationError({"name": "O nome é obrigatório."})
    price = Decimal(price)
    if price <= 0:
        raise ValidationError({"price": "O valor deve ser maior que zero."})
    services = Service.objects.for_tenant(tenant).filter(pk__in=service_ids or [])
    if not services.exists():
        raise ValidationError({"services": "Selecione ao menos um serviço para o pacote."})
    package = Package.objects.create(
        tenant=tenant, name=name, description=(description or "").strip(), price=price,
        generates_commission=bool(generates_commission), created_by=created_by,
    )
    package.services.set(services)
    return package


def update_package(package, *, name, description="", price, service_ids, generates_commission):
    from apps.services.models import Service

    name = str(name).strip()
    if not name:
        raise ValidationError({"name": "O nome é obrigatório."})
    price = Decimal(price)
    if price <= 0:
        raise ValidationError({"price": "O valor deve ser maior que zero."})
    services = Service.objects.for_tenant(package.tenant).filter(pk__in=service_ids or [])
    if not services.exists():
        raise ValidationError({"services": "Selecione ao menos um serviço para o pacote."})
    package.name = name
    package.description = (description or "").strip()
    package.price = price
    package.generates_commission = bool(generates_commission)
    package.save(update_fields=["name", "description", "price", "generates_commission"])
    package.services.set(services)
    return package


def set_package_active(package, is_active):
    package.is_active = bool(is_active)
    package.save(update_fields=["is_active"])
    return package


@transaction.atomic
def assign_package_to_client(client, *, package, payment_method, created_by):
    """Ativa um pacote pro cliente — pergunta a forma de pagamento porque é
    uma cobrança REAL de verdade agora (mesmo tratamento de
    `add_client_credit`): cria a `CashTransaction` da mensalidade, marca o
    cliente como mensalista e empurra o vencimento pra daqui a 1 mês (mesma
    conta de `renew_subscription`). Os atendimentos cobertos pelo pacote,
    dali em diante, é que não geram nova cobrança (ver
    `apps.scheduling.services.complete_appointment`)."""
    from apps.finance.models import CashCategory, CashFlowType
    from apps.finance.services import create_cash_transaction

    if package.tenant_id != client.tenant_id:
        raise ValidationError("Pacote e cliente precisam ser do mesmo salão.")
    if not package.is_active:
        raise ValidationError("Este pacote não está mais disponível.")

    create_cash_transaction(
        tenant=client.tenant,
        type=CashFlowType.IN,
        category=CashCategory.PACKAGE_SALE,
        amount=package.price,
        payment_method=payment_method,
        description=f"Pacote {package.name} — {client.name}",
        created_by=created_by,
    )
    client.package = package
    client.is_subscriber = True
    client.subscription_due_date = _add_months(datetime.date.today(), 1)
    client.save(update_fields=["package", "is_subscriber", "subscription_due_date"])
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


# ---------------------------------------------------------------------------
# Alerta de aniversário (decisão do usuário em 2026-08-04)
# ---------------------------------------------------------------------------


def clients_with_birthday_today(tenant):
    from django.utils import timezone

    today = timezone.localdate()
    return Client.objects.for_tenant(tenant).filter(
        birth_day=today.day, birth_month=today.month
    ).order_by("name")


def pending_birthday_clients_today(tenant):
    """Aniversariantes de hoje que ainda NÃO receberam a mensagem de
    parabéns (ver `mark_birthday_message_sent`) — é essa lista que aparece
    no modal de envio e que decide se o alerta do sininho continua ativo."""
    from django.utils import timezone

    today = timezone.localdate()
    return clients_with_birthday_today(tenant).exclude(last_birthday_greeted_on=today)


def mark_birthday_message_sent(client):
    """Registra que a mensagem de parabéns de hoje foi enviada a este
    cliente (clique em "Enviar" no modal, não só abrir a lista) e, se não
    sobrar mais nenhum aniversariante pendente, encerra o alerta do dia no
    sininho — decisão do usuário em 2026-08-05."""
    from django.utils import timezone

    today = timezone.localdate()
    client.last_birthday_greeted_on = today
    client.save(update_fields=["last_birthday_greeted_on"])
    _close_birthday_notification_if_all_greeted(client.tenant)
    return client


def _close_birthday_notification_if_all_greeted(tenant):
    if pending_birthday_clients_today(tenant).exists():
        return

    from django.utils import timezone

    from apps.notifications.models import TenantNotification, TenantNotificationKind

    today = timezone.localdate()
    TenantNotification.objects.for_tenant(tenant).filter(
        kind=TenantNotificationKind.BIRTHDAY_ALERT, is_read=False, created_at__date=today,
    ).update(is_read=True, read_at=timezone.now())


def ensure_birthday_notification(tenant):
    """Garante (idempotente) UMA `TenantNotification` por dia quando há
    aniversariante PENDENTE (ainda sem mensagem enviada) — chamado a cada
    poll do sininho/toast (`apps.notifications.views.agenda_toast_poll`),
    sem precisar de tarefa agendada (Celery) separada. Só cria se
    `Tenant.birthday_alert_enabled`; a notificação some do sininho quando a
    mensagem É ENVIADA de fato pra todos (`mark_birthday_message_sent`), não
    só por abrir o modal — decisão do usuário em 2026-08-05."""
    if not tenant.birthday_alert_enabled:
        return None

    from django.utils import timezone

    from apps.notifications.models import TenantNotification, TenantNotificationKind
    from apps.notifications.services import create_tenant_notification

    clients = list(pending_birthday_clients_today(tenant))
    if not clients:
        return None

    today = timezone.localdate()
    already_created_today = TenantNotification.objects.for_tenant(tenant).filter(
        kind=TenantNotificationKind.BIRTHDAY_ALERT, created_at__date=today,
    ).exists()
    if already_created_today:
        return None

    names = ", ".join(c.name for c in clients)
    if len(clients) == 1:
        title = "Aniversariante do dia! 🎂"
        message = f"{names} faz aniversário hoje. Que tal mandar uma mensagem?"
    else:
        title = f"{len(clients)} aniversariantes hoje! 🎂"
        message = f"{names} fazem aniversário hoje. Que tal mandar uma mensagem?"

    return create_tenant_notification(
        tenant, kind=TenantNotificationKind.BIRTHDAY_ALERT, title=title, message=message,
    )
