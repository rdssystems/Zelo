import datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import asaas_client
from .models import AsaasWebhookEvent, Plan, Subscription, SubscriptionStatus

SUBSCRIPTION_PERIOD_DAYS = 30


def create_plan(*, name, price, description="", is_active=True, order=0):
    return Plan.objects.create(
        name=name, price=price, description=description, is_active=is_active, order=order
    )


def update_plan(plan, *, name, price, description, is_active, order):
    plan.name = name
    plan.price = price
    plan.description = description
    plan.is_active = is_active
    plan.order = order
    plan.save()
    return plan


TRIAL_DAYS = 14


def create_subscription_for_tenant(tenant):
    """Chamado no cadastro de um tenant novo (`apps/tenants/services.py::register_tenant`) —
    nasce em teste com acesso completo por `TRIAL_DAYS` dias corridos, sem plano
    definido até o tenant assinar um (ou o superadmin atribuir manualmente)."""
    return Subscription.objects.create(
        tenant=tenant,
        status=SubscriptionStatus.TRIALING,
        trial_ends_at=timezone.now() + datetime.timedelta(days=TRIAL_DAYS),
    )


def change_subscription_plan(subscription, plan):
    """Atribuir/trocar plano sempre reinicia a contagem de 30 dias corridos
    (decisão do usuário em 2026-07-29) — período fica em aberto (None) quando
    o plano é removido. Assinatura com cobrança recorrente (Asaas) é
    controlada pelo webhook, não por aqui (ver `Subscription.is_recurring`)."""
    if subscription.is_recurring:
        raise ValidationError(
            "Esta assinatura tem pagamento recorrente ativo — o plano é controlado "
            "automaticamente pela cobrança e não pode ser trocado manualmente."
        )
    subscription.plan = plan
    if plan is not None:
        today = timezone.localdate()
        subscription.current_period_start = today
        subscription.current_period_end = today + datetime.timedelta(days=SUBSCRIPTION_PERIOD_DAYS)
    else:
        subscription.current_period_start = None
        subscription.current_period_end = None
    subscription.save(
        update_fields=["plan", "current_period_start", "current_period_end", "updated_at"]
    )
    return subscription


def update_subscription_period(subscription, *, current_period_start, current_period_end):
    """Ajuste manual das datas do período pelo superadmin (ex: cortesia,
    correção). Bloqueado para assinatura com cobrança recorrente ativa —
    nesse caso as datas vêm do Asaas e mexer aqui desconfiguraria o plano do
    cliente no pagamento."""
    if subscription.is_recurring:
        raise ValidationError(
            "Esta assinatura tem pagamento recorrente ativo — as datas do período são "
            "controladas automaticamente pela cobrança e não podem ser editadas manualmente."
        )
    if current_period_end < current_period_start:
        raise ValidationError("A data fim deve ser depois da data início.")
    subscription.current_period_start = current_period_start
    subscription.current_period_end = current_period_end
    subscription.save(update_fields=["current_period_start", "current_period_end", "updated_at"])
    return subscription


def change_subscription_status(subscription, status):
    subscription.status = status
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


# ---------------------------------------------------------------------------
# RF30 — bloqueio de painel por inadimplência
# ---------------------------------------------------------------------------


def grace_deadline(subscription):
    """Último dia em que o acesso continua liberado pra uma assinatura
    `overdue` — `None` se não há `current_period_end` conhecido (nesse caso
    `subscription_blocks_panel_access` bloqueia direto, não há o que contar)."""
    if not subscription.current_period_end:
        return None
    return subscription.current_period_end + datetime.timedelta(
        days=subscription.grace_period_days
    )


def subscription_blocks_panel_access(tenant):
    """RF30 (decisão do usuário em 2026-07-31): `canceled` bloqueia o painel
    na hora; `overdue` só bloqueia depois de estourado o `grace_period_days`
    contado do fim do último período pago (`current_period_end`) — sem
    período conhecido, bloqueia direto (mais seguro que deixar passar sem
    limite). `trialing` bloqueia assim que `trial_ends_at` passa — sem
    tolerância extra (decisão do usuário em 2026-07-31: a contagem
    regressiva já fica visível o tempo todo no menu lateral/Meu Plano,
    então não é surpresa; `grace_period_days` continua exclusivo de
    assinatura paga vencida). Sem `trial_ends_at` setado (não deveria
    acontecer — `create_subscription_for_tenant` sempre seta — mas por
    segurança) não bloqueia, diferente do caso overdue sem período: aqui a
    ausência do dado é uma anomalia que não deve penalizar o tenant.
    `pending`/`active` nunca bloqueiam.

    Usado pelos decorators de painel (`apps.accounts.decorators`) — a
    página pública de agendamento (`/<slug>/`) nunca é afetada, só o painel
    administrativo do salão."""
    try:
        subscription = tenant.subscription
    except Subscription.DoesNotExist:
        return False
    if subscription.status == SubscriptionStatus.CANCELED:
        return True
    if subscription.status == SubscriptionStatus.OVERDUE:
        deadline = grace_deadline(subscription)
        if deadline is None:
            return True
        return timezone.localdate() > deadline
    if subscription.status == SubscriptionStatus.TRIALING:
        if not subscription.trial_ends_at:
            return False
        return timezone.now() > subscription.trial_ends_at
    return False


# ---------------------------------------------------------------------------
# Checkout self-service (tenant assina de dentro do painel) — /painel/plano/
# ---------------------------------------------------------------------------


def select_plan(subscription, plan):
    """1º passo do fluxo de assinatura: só grava a intenção (qual plano o
    tenant escolheu) — ainda não fala com o Asaas. `get_or_create_checkout_url`
    é quem de fato cria a cobrança."""
    if subscription.status == SubscriptionStatus.ACTIVE:
        raise ValidationError("Esta conta já tem uma assinatura ativa.")
    subscription.plan = plan
    subscription.save(update_fields=["plan", "updated_at"])
    return subscription


def get_or_create_checkout_url(subscription, *, admin_email=""):
    """Garante uma assinatura no Asaas pro plano escolhido e devolve a URL da
    fatura (`invoiceUrl`) pra exibir no painel (iframe, com link de abrir em
    nova aba como alternativa — ver `painel/billing/checkout.html`).

    Reaproveita a assinatura do Asaas já criada se o tenant voltar a essa
    tela antes de pagar (evita duplicar cobrança a cada refresh) — só cria
    uma nova se a anterior não tiver fatura disponível.
    """
    tenant = subscription.tenant
    if subscription.status == SubscriptionStatus.ACTIVE:
        raise ValidationError("Esta conta já tem uma assinatura ativa.")
    if not subscription.plan:
        raise ValidationError("Escolha um plano antes de assinar.")
    if not tenant.document:
        raise ValidationError(
            "Informe o CPF/CNPJ do salão antes de assinar — o Asaas exige isso "
            "pra emitir a cobrança."
        )

    if subscription.asaas_subscription_id and subscription.status == SubscriptionStatus.PENDING:
        invoice_url = asaas_client.get_first_invoice_url(subscription.asaas_subscription_id)
        if invoice_url:
            return invoice_url
        # Assinatura no Asaas existe mas ainda sem fatura gerada (ou órfã) —
        # segue o fluxo abaixo e cria uma nova.

    if not subscription.asaas_customer_id:
        customer = asaas_client.create_customer(
            name=tenant.name,
            cpf_cnpj=tenant.document,
            email=admin_email,
            external_reference=str(tenant.id),
        )
        subscription.asaas_customer_id = customer["id"]

    asaas_subscription = asaas_client.create_subscription(
        customer_id=subscription.asaas_customer_id,
        value=subscription.plan.price,
        next_due_date=timezone.localdate(),
        description=f"Zelo — plano {subscription.plan.name}",
        external_reference=str(tenant.id),
    )
    subscription.asaas_subscription_id = asaas_subscription["id"]
    subscription.status = SubscriptionStatus.PENDING
    subscription.save(
        update_fields=["asaas_customer_id", "asaas_subscription_id", "status", "updated_at"]
    )

    invoice_url = asaas_client.get_first_invoice_url(subscription.asaas_subscription_id)
    if not invoice_url:
        raise asaas_client.AsaasError(
            "Assinatura criada no Asaas, mas a fatura ainda não ficou pronta — "
            "tente atualizar a página em alguns segundos."
        )
    return invoice_url


# ---------------------------------------------------------------------------
# Webhook do Asaas — POST /webhooks/asaas/
# ---------------------------------------------------------------------------

_ACTIVATING_EVENTS = {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}
_OVERDUE_EVENTS = {"PAYMENT_OVERDUE"}
_CANCELING_EVENTS = {"PAYMENT_DELETED", "PAYMENT_REFUNDED", "SUBSCRIPTION_DELETED"}


def _record_webhook_event(payment_id, event_type):
    """`True` se este evento ainda não tinha sido processado (deve seguir);
    `False` se é reenvio do Asaas de um evento já tratado (idempotência —
    Asaas reenvia se não recebe 200 a tempo, ver `AsaasWebhookEvent`).

    O `transaction.atomic()` aqui dentro é necessário mesmo fora de testes:
    sem ele, o `IntegrityError` da constraint duplicada deixa a transação
    "quebrada" no Postgres (qualquer query seguinte falharia com
    "current transaction is aborted") — o `atomic()` cria um savepoint só
    pra esse insert, então só ele é desfeito no conflito."""
    try:
        with transaction.atomic():
            AsaasWebhookEvent.objects.create(payment_id=payment_id, event_type=event_type)
    except IntegrityError:
        return False
    return True


def handle_asaas_webhook(*, event_type, payment_data):
    """Processa 1 evento de payment do webhook do Asaas. Ignorado (sem erro)
    se: evento duplicado, sem `subscription` vinculada (cobrança avulsa), ou
    `asaas_subscription_id` não bate com nenhuma Subscription local (ex:
    evento de outro ambiente/teste)."""
    payment_id = payment_data.get("id", "")
    if not _record_webhook_event(payment_id, event_type):
        return

    asaas_subscription_id = payment_data.get("subscription")
    if not asaas_subscription_id:
        return

    try:
        subscription = Subscription.objects.select_related("tenant").get(
            asaas_subscription_id=asaas_subscription_id
        )
    except Subscription.DoesNotExist:
        return

    if event_type in _ACTIVATING_EVENTS:
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.current_period_start = timezone.localdate()
        subscription.current_period_end = timezone.localdate() + datetime.timedelta(
            days=SUBSCRIPTION_PERIOD_DAYS
        )
        subscription.save(
            update_fields=["status", "current_period_start", "current_period_end", "updated_at"]
        )
    elif event_type in _OVERDUE_EVENTS:
        subscription.status = SubscriptionStatus.OVERDUE
        subscription.save(update_fields=["status", "updated_at"])
    elif event_type in _CANCELING_EVENTS:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.save(update_fields=["status", "updated_at"])
