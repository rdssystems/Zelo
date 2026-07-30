import datetime
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.finance.models import CashFlowType
from apps.tenants.models import TenantModel


class Client(TenantModel):
    """Cliente final, identificado por telefone dentro do tenant (RF04).

    CRM: além da identificação, guarda preferências (texto livre), status de
    mensalista com data de vencimento, e o saldo da carteira de crédito.
    """

    phone = models.CharField("telefone", max_length=20)
    name = models.CharField("nome", max_length=120)
    preferences = models.TextField(
        "preferências",
        blank=True,
        help_text="Alergias, produtos preferidos, observações do atendimento.",
    )
    is_subscriber = models.BooleanField("mensalista", default=False)
    subscription_due_date = models.DateField(
        "vencimento da mensalidade", null=True, blank=True
    )
    # Derivado — NUNCA editar direto (CLAUDE.md regra 2, mesma regra de
    # Product.current_stock). Só muda via apps/clients/services.py
    # (add/remove/redeem_client_credit), sempre com um
    # ClientCreditTransaction registrando o motivo.
    credit_balance = models.DecimalField(
        "saldo de crédito", max_digits=10, decimal_places=2, default=Decimal("0")
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "cliente"
        verbose_name_plural = "clientes"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "phone"], name="unique_client_phone_per_tenant"
            ),
        ]
        indexes = [models.Index(fields=["tenant", "phone"])]

    def __str__(self):
        return f"{self.name} ({self.phone})"

    @property
    def whatsapp_url(self):
        """`None` para telefone anonimizado via LGPD (`remove_client_data`,
        formato `removido-{pk}`) — só dígitos vira link `wa.me` com DDI 55."""
        if not self.phone.isdigit():
            return None
        return f"https://wa.me/55{self.phone}"

    @property
    def subscription_is_overdue(self):
        return bool(
            self.is_subscriber
            and self.subscription_due_date
            and self.subscription_due_date < datetime.date.today()
        )

    @property
    def subscription_is_due_soon(self):
        """Vence nos próximos 7 dias (inclusive hoje)."""
        if not (self.is_subscriber and self.subscription_due_date):
            return False
        today = datetime.date.today()
        return today <= self.subscription_due_date <= today + datetime.timedelta(days=7)


class ClientCreditTransaction(TenantModel):
    """Movimentação da carteira de crédito do cliente — cada uma recalcula
    `Client.credit_balance` (mesmo padrão de StockMovement/current_stock).

    Recarga (IN) sempre nasce vinculada a uma `CashTransaction` real (o
    dinheiro entrou no caixa naquele momento). Uso em atendimento (OUT) fica
    vinculado ao `Appointment` e NÃO gera nova entrada de caixa — a receita
    já foi reconhecida na recarga.
    """

    client = models.ForeignKey(
        Client,
        # PROTECT: histórico da carteira é auditoria financeira — nunca some
        # junto com o cliente (a anonimização LGPD preserva o ledger).
        on_delete=models.PROTECT,
        related_name="credit_transactions",
    )
    # Reaproveita o enum de apps.finance (IN=creditado, OUT=usado/removido).
    type = models.CharField("tipo", max_length=10, choices=CashFlowType.choices)
    amount = models.DecimalField(
        "valor",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    reason = models.CharField("motivo", max_length=255)
    related_appointment = models.ForeignKey(
        "scheduling.Appointment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="credit_transactions",
    )
    related_cash_transaction = models.ForeignKey(
        "finance.CashTransaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="credit_transactions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "movimentação de crédito"
        verbose_name_plural = "movimentações de crédito"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "client", "created_at"])]

    def __str__(self):
        return f"{self.type} — {self.client} — R$ {self.amount}"
