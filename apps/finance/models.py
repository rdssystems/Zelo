from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.employees.models import CommissionType
from apps.tenants.models import TenantModel


class CashFlowType(models.TextChoices):
    IN = "in", "Entrada"
    OUT = "out", "Saída"


class CashCategory(models.TextChoices):
    SERVICE_SALE = "service_sale", "Venda de serviço"
    PRODUCT_SALE = "product_sale", "Venda de produto"
    COMMISSION_PAYMENT = "commission_payment", "Pagamento de comissão"
    # Recarga da carteira de crédito do cliente: entrada REAL de dinheiro no
    # momento da recarga. O uso posterior do crédito NÃO gera nova entrada
    # (ver apps/clients/services.py — evita contar a receita duas vezes).
    CLIENT_CREDIT_TOPUP = "client_credit_topup", "Recarga de crédito"
    # Cobrança da mensalidade de um pacote (decisão do usuário em 2026-08-04)
    # — entrada REAL no momento em que o pacote é atribuído ao cliente. Os
    # atendimentos cobertos por ele depois NÃO geram nova CashTransaction
    # (já pago aqui) — ver apps.scheduling.services.complete_appointment.
    PACKAGE_SALE = "package_sale", "Venda de pacote"
    EXPENSE = "expense", "Despesa"
    OTHER = "other", "Outro"


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Dinheiro"
    PIX = "pix", "Pix"
    CREDIT_CARD = "credit_card", "Cartão de crédito"
    DEBIT_CARD = "debit_card", "Cartão de débito"
    # Pagamento com o saldo da carteira do cliente — na conclusão de
    # atendimento, esta forma NÃO cria CashTransaction (o dinheiro já entrou
    # na recarga); só abate Client.credit_balance via ledger próprio.
    CLIENT_CREDIT = "client_credit", "Crédito do cliente"
    OTHER = "other", "Outro"


class CommissionStatus(models.TextChoices):
    PENDING = "pending", "Pendente"
    PAID = "paid", "Paga"


class CashTransaction(TenantModel):
    """Lançamento de caixa (RF21) — sempre amarrado a uma origem rastreável
    (agendamento, movimentação de estoque, comissão ou despesa avulsa)."""

    type = models.CharField("tipo", max_length=10, choices=CashFlowType.choices)
    category = models.CharField("categoria", max_length=20, choices=CashCategory.choices)
    amount = models.DecimalField(
        "valor", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    payment_method = models.CharField(
        "forma de pagamento", max_length=20, choices=PaymentMethod.choices
    )
    description = models.CharField("descrição", max_length=255, blank=True)
    # PROTECT: um lançamento de caixa nunca pode perder o rastro da origem
    # (RNF05 — auditoria mínima de dado financeiro).
    related_appointment = models.ForeignKey(
        "scheduling.Appointment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cash_transactions",
    )
    related_stock_movement = models.ForeignKey(
        "inventory.StockMovement",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cash_transactions",
    )
    related_commission = models.ForeignKey(
        "Commission",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cash_transactions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "lançamento de caixa"
        verbose_name_plural = "lançamentos de caixa"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "category", "created_at"])]

    def __str__(self):
        return f"{self.get_type_display()} — {self.get_category_display()} — R$ {self.amount}"


class Commission(TenantModel):
    """Comissão gerada ao concluir um atendimento (RF16). Snapshot do tipo e
    valor usados no cálculo — nunca recalcular depois com dado que mudou."""

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="commissions"
    )
    appointment = models.OneToOneField(
        "scheduling.Appointment", on_delete=models.PROTECT, related_name="commission"
    )
    commission_type = models.CharField(
        "tipo (snapshot)", max_length=20, choices=CommissionType.choices
    )
    commission_value = models.DecimalField(
        "valor/percentual (snapshot)", max_digits=10, decimal_places=2
    )
    base_amount = models.DecimalField(
        "valor base do serviço", max_digits=10, decimal_places=2
    )
    calculated_amount = models.DecimalField(
        "valor calculado da comissão", max_digits=10, decimal_places=2
    )
    status = models.CharField(
        "status", max_length=10, choices=CommissionStatus.choices, default=CommissionStatus.PENDING
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    # Quando a comissão foi de fato gerada (conclusão do atendimento) — não
    # confundir com `appointment.date` (data AGENDADA do serviço). Um
    # atendimento pode ser concluído antes ou depois da data agendada (ex.:
    # cliente chega adiantado e o salão atende na hora, comanda aberta num
    # dia e fechada só no seguinte) — filtro de período no Caixa/Comissões
    # usa este campo, igual `CashTransaction.created_at`, pra bater com o
    # dinheiro que realmente moveu no período (bug real encontrado: comissão
    # de atendimento concluído adiantado sumia da aba Comissões porque o
    # filtro de período usava `appointment__date`, no futuro em relação a
    # hoje — a `CashTransaction` da mesma venda já usava `created_at` e
    # aparecia normalmente, gerando a inconsistência).
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "comissão"
        verbose_name_plural = "comissões"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "employee", "status"])]

    def __str__(self):
        return f"{self.employee} — R$ {self.calculated_amount} ({self.get_status_display()})"


class ComandaProductItem(TenantModel):
    """Produto adicionado a uma comanda em andamento, ainda NÃO vendido de
    verdade (sem `StockMovement`/`CashTransaction` ainda) — só vira venda
    real quando a comanda é finalizada (`complete_client_comanda`).

    Existir como registro no banco (e não só em memória no navegador) é o
    que garante que o item sobrevive a trocar de aba/página antes de fechar
    a conta — bug relatado pelo usuário quando o carrinho era só
    `Alpine.store` (perdido a cada navegação)."""

    client = models.ForeignKey(
        "clients.Client", on_delete=models.CASCADE, related_name="pending_comanda_items"
    )
    # PROTECT: item pendente não pode ficar órfão se o produto for excluído
    # (não deveria nem ser possível excluir produto com pendência, mas a
    # FK protege mesmo assim).
    product = models.ForeignKey(
        "inventory.Product", on_delete=models.PROTECT, related_name="pending_comanda_items"
    )
    quantity = models.DecimalField(
        "quantidade", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "item pendente de comanda"
        verbose_name_plural = "itens pendentes de comanda"
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "product"], name="unique_pending_item_per_client_product"
            ),
        ]

    def __str__(self):
        return f"{self.client} — {self.product} x{self.quantity} (pendente)"
