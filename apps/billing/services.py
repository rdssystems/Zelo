import datetime

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Plan, Subscription, SubscriptionStatus

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


def create_subscription_for_tenant(tenant):
    """Chamado no cadastro de um tenant novo (`apps/tenants/services.py::register_tenant`) —
    nasce em teste, sem plano definido até o superadmin atribuir um."""
    return Subscription.objects.create(tenant=tenant, status=SubscriptionStatus.TRIALING)


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
