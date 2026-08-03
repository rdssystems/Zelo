from django.utils import timezone

from .models import Subscription, SubscriptionStatus


def sidebar_plan(request):
    """Injeta `sidebar_plan_label` e `sidebar_plan_days_left` no menu lateral
    do painel (embaixo do e-mail/nome do salão) — só pro admin do salão
    (decisão do usuário em 2026-07-31), mesmo critério do sininho de avisos
    (`apps.notifications.context_processors.unread_announcements`).

    Só conta como plano pago (mostra o nome do plano + dias do período) com
    `status == ACTIVE` — webhook do Asaas confirmou o pagamento. Escolher um
    plano mas não pagar deixa `subscription.plan` preenchido com `status`
    `pending` (ver `apps.billing.services.get_or_create_checkout_url`); até
    a confirmação chegar, o salão continua no modo gratuito, contando os
    `TRIAL_DAYS` (`apps.billing.services`) a partir de `trial_ends_at`
    (decisão do usuário em 2026-07-31 — "não pagou, é gratuito").
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or user.role != "tenant_admin":
        return {}
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return {}
    try:
        subscription = Subscription.objects.select_related("plan").get(tenant=tenant)
    except Subscription.DoesNotExist:
        return {}

    is_expired = False
    if subscription.plan and subscription.status == SubscriptionStatus.ACTIVE:
        label = subscription.plan.name
        days_left = None
        if subscription.current_period_end:
            days_left = max(0, (subscription.current_period_end - timezone.localdate()).days)
    else:
        label = "Gratuito"
        days_left = None
        if subscription.trial_ends_at:
            # `trial_ends_at` é datetime timezone-aware — `.date()" direto
            # extrai a data em UTC, não na TIME_ZONE do projeto
            # (America/Sao_Paulo), o que descasa com `timezone.localdate()`
            # bem na janela em que UTC já virou o dia mas São Paulo ainda
            # não (idem em `apps/billing/views.py::my_plan`). `localtime()`
            # converte pro fuso local antes de extrair a data.
            raw_days = (timezone.localtime(subscription.trial_ends_at).date() - timezone.localdate()).days
            days_left = max(0, raw_days)
            is_expired = raw_days < 0

    return {
        "sidebar_plan_label": label,
        "sidebar_plan_days_left": days_left,
        "sidebar_plan_expired": is_expired,
    }
