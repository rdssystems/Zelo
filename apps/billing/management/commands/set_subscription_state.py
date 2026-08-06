"""Ferramenta de dev pra colocar a assinatura de um tenant num estado
específico e testar o fluxo de cobrança (RF30 — bloqueio de painel) sem
esperar dias reais passarem ou reconfigurar manualmente pelo shell toda vez.

Uso:
  docker compose exec web python manage.py set_subscription_state <tenant-ou-email> <estado> [--days N] [--plan "Nome"]

Exemplos:
  set_subscription_state klismanrds90@gmail.com trial_expired
  set_subscription_state klismanrds90@gmail.com trial_active --days 3
  set_subscription_state klismanrds90@gmail.com active --plan "Studio"
  set_subscription_state klismanrds90@gmail.com overdue_in_grace
  set_subscription_state klismanrds90@gmail.com overdue_expired
  set_subscription_state klismanrds90@gmail.com canceled
  set_subscription_state klismanrds90@gmail.com pending --plan "Studio"
"""
import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.billing.models import Plan, Subscription, SubscriptionStatus
from apps.billing.services import TRIAL_DAYS
from apps.tenants.models import Tenant

User = get_user_model()

STATES = [
    "trial_active",
    "trial_expired",
    "pending",
    "active",
    "overdue_in_grace",
    "overdue_expired",
    "canceled",
]


class Command(BaseCommand):
    help = (
        "Coloca a assinatura de um tenant num estado específico (trial vencido, plano "
        "ativo, atrasado dentro/fora da carência, cancelado...), pra testar o bloqueio "
        "de painel sem esperar. Só roda com DJANGO_DEBUG=True."
    )

    def add_arguments(self, parser):
        parser.add_argument("tenant", help="Slug do tenant ou e-mail de um usuário dele")
        parser.add_argument("state", choices=STATES)
        parser.add_argument(
            "--days", type=int, default=None,
            help="Dias à frente/atrás, dependendo do estado (cada estado tem um default sensato).",
        )
        parser.add_argument(
            "--plan", default=None,
            help="Nome do Plan a atribuir — obrigatório pra 'active', opcional pros demais.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "set_subscription_state só pode rodar com DJANGO_DEBUG=True — é "
                "ferramenta de teste local, nunca produção."
            )

        tenant = self._resolve_tenant(options["tenant"])
        subscription, _ = Subscription.objects.get_or_create(tenant=tenant)
        state = options["state"]
        days = options["days"]
        if options["plan"]:
            subscription.plan = self._resolve_plan(options["plan"])

        now = timezone.now()
        today = timezone.localdate()

        if state == "trial_active":
            subscription.status = SubscriptionStatus.TRIALING
            subscription.trial_ends_at = now + datetime.timedelta(
                days=days if days is not None else TRIAL_DAYS
            )
        elif state == "trial_expired":
            subscription.status = SubscriptionStatus.TRIALING
            subscription.trial_ends_at = now - datetime.timedelta(days=days if days is not None else 1)
        elif state == "pending":
            subscription.status = SubscriptionStatus.PENDING
        elif state == "active":
            if not subscription.plan:
                raise CommandError("Estado 'active' precisa de --plan \"Nome do Plano\".")
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.current_period_start = today
            subscription.current_period_end = today + datetime.timedelta(days=days if days is not None else 30)
        elif state == "overdue_in_grace":
            subscription.status = SubscriptionStatus.OVERDUE
            subscription.current_period_end = today - datetime.timedelta(days=days if days is not None else 1)
        elif state == "overdue_expired":
            subscription.status = SubscriptionStatus.OVERDUE
            grace = subscription.grace_period_days or 0
            subscription.current_period_end = today - datetime.timedelta(
                days=days if days is not None else grace + 2
            )
        elif state == "canceled":
            subscription.status = SubscriptionStatus.CANCELED

        subscription.save()
        self.stdout.write(self.style.SUCCESS(
            f"{tenant.name} ({tenant.slug}): status={subscription.status}, "
            f"plan={subscription.plan}, trial_ends_at={subscription.trial_ends_at}, "
            f"current_period_end={subscription.current_period_end}, "
            f"grace_period_days={subscription.grace_period_days}"
        ))

    def _resolve_tenant(self, identifier):
        tenant = Tenant.objects.filter(slug=identifier).first()
        if tenant:
            return tenant
        user = User.objects.filter(email=identifier).select_related("tenant").first()
        if user and user.tenant:
            return user.tenant
        raise CommandError(f"Não achei tenant nem usuário com tenant pra '{identifier}'.")

    def _resolve_plan(self, name):
        plan = Plan.objects.filter(name=name).first()
        if not plan:
            available = ", ".join(Plan.objects.values_list("name", flat=True)) or "(nenhum cadastrado)"
            raise CommandError(f"Plano '{name}' não encontrado. Disponíveis: {available}")
        return plan
