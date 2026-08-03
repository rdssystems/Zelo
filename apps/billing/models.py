from django.core.validators import MinValueValidator
from django.db import models

from apps.tenants.models import Tenant


class Plan(models.Model):
    """Plano de assinatura da plataforma (superadmin gerencia em /plataforma/)."""

    name = models.CharField("nome", max_length=60, unique=True)
    description = models.CharField("descrição", max_length=255, blank=True)
    price = models.DecimalField(
        "preço mensal", max_digits=8, decimal_places=2, validators=[MinValueValidator(0)]
    )
    is_active = models.BooleanField("ativo", default=True)
    order = models.PositiveSmallIntegerField("ordem de exibição", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "plano"
        verbose_name_plural = "planos"
        ordering = ["order", "price"]

    def __str__(self):
        return self.name


class SubscriptionStatus(models.TextChoices):
    TRIALING = "trialing", "Em teste"
    PENDING = "pending", "Aguardando pagamento"
    ACTIVE = "active", "Ativa"
    OVERDUE = "overdue", "Inadimplente"
    CANCELED = "canceled", "Cancelada"


class Subscription(models.Model):
    """Assinatura da plataforma por tenant (1:1).

    Controle manual por enquanto — Etapa 9 (Asaas) foi deliberadamente
    adiada pelo usuário; os campos `asaas_*` já ficam reservados pra quando
    a integração automática existir, mas hoje só o superadmin muda
    `status`/`plan` manualmente em /plataforma/.
    """

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(
        Plan, on_delete=models.PROTECT, null=True, blank=True, related_name="subscriptions"
    )
    status = models.CharField(
        "status", max_length=10, choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.TRIALING,
    )
    asaas_customer_id = models.CharField(max_length=60, blank=True)
    asaas_subscription_id = models.CharField(max_length=60, blank=True)
    trial_ends_at = models.DateTimeField(
        "fim do período de teste", null=True, blank=True,
        help_text="14 dias corridos a partir do cadastro (register_tenant) — acesso completo até aqui.",
    )
    current_period_start = models.DateField("início do período atual", null=True, blank=True)
    current_period_end = models.DateField("fim do período atual", null=True, blank=True)
    grace_period_days = models.PositiveSmallIntegerField("dias de tolerância", default=5)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "assinatura"
        verbose_name_plural = "assinaturas"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tenant.name} — {self.get_status_display()}"

    @property
    def is_recurring(self):
        """Cobrança automática via Asaas — quando existe, as datas do período
        são controladas pelo webhook, não pelo superadmin, então o painel
        bloqueia edição manual (ver
        `apps.billing.services.update_subscription_period`)."""
        return bool(self.asaas_subscription_id)


class AsaasWebhookEvent(models.Model):
    """Log de dedupe dos webhooks recebidos do Asaas — a Asaas reenvia o
    mesmo evento se não receber 200 a tempo, então processar sem checar
    duplicidade poderia reativar/cobrar duas vezes. `unique_together` é a
    barreira: a 2ª tentativa de gravar o mesmo (payment_id, event_type) falha
    e a view simplesmente responde 200 sem reprocessar."""

    payment_id = models.CharField("id do payment no Asaas", max_length=60)
    event_type = models.CharField("tipo do evento", max_length=40)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "evento de webhook Asaas"
        verbose_name_plural = "eventos de webhook Asaas"
        constraints = [
            models.UniqueConstraint(
                fields=["payment_id", "event_type"], name="unique_asaas_webhook_event"
            ),
        ]

    def __str__(self):
        return f"{self.event_type} — {self.payment_id}"
