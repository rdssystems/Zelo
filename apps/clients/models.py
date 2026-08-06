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
    # Pacote de mensalidade ativo (decisão do usuário em 2026-08-04) — camada
    # em cima do mensalista tradicional acima (vencimento/renovação
    # continuam funcionando do mesmo jeito). Define quais serviços o cliente
    # já tem incluídos sem cobrança extra — ver `apps.clients.services.
    # assign_package_to_client`. PROTECT: histórico de agendamento
    # (`Appointment.package`) referencia o pacote vigente em cada um.
    package = models.ForeignKey(
        "Package", on_delete=models.PROTECT, null=True, blank=True,
        related_name="subscribed_clients",
    )
    # Só dia/mês (sem ano) — decisão do usuário em 2026-08-04: o objetivo é só
    # gerar o alerta de aniversário (ver apps.clients.services.
    # clients_with_birthday_today), não guardar idade. Sempre os dois juntos
    # ou nenhum — validado em apps/clients/services.py.
    birth_day = models.PositiveSmallIntegerField("dia de nascimento", null=True, blank=True)
    birth_month = models.PositiveSmallIntegerField("mês de nascimento", null=True, blank=True)
    # Marca a última vez que a mensagem de parabéns foi ENVIADA (clique em
    # "Enviar" no modal), não quando o modal foi aberto — decisão do usuário
    # em 2026-08-05: abrir a lista não deve fazer o alerta sumir do sininho,
    # só o envio de fato (ver apps.clients.services.mark_birthday_message_sent).
    last_birthday_greeted_on = models.DateField(
        "último parabéns enviado em", null=True, blank=True
    )
    # Derivado — NUNCA editar direto (CLAUDE.md regra 2, mesma regra de
    # Product.current_stock). Só muda via apps/clients/services.py
    # (add/remove/redeem_client_credit), sempre com um
    # ClientCreditTransaction registrando o motivo.
    credit_balance = models.DecimalField(
        "saldo de crédito", max_digits=10, decimal_places=2, default=Decimal("0")
    )
    # Derivado — NUNCA editar direto (mesma regra do credit_balance acima).
    # Só muda via apps/clients/services.py (record/settle/write_off_client_debt),
    # sempre com um ClientDebtTransaction registrando o motivo.
    debt_balance = models.DecimalField(
        "saldo devedor", max_digits=10, decimal_places=2, default=Decimal("0")
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
        """Vence dentro da janela configurada em
        `Tenant.subscription_due_soon_days` (Configurações, decisão do
        usuário em 2026-07-31 — antes era um `7` fixo aqui)."""
        if not (self.is_subscriber and self.subscription_due_date):
            return False
        today = datetime.date.today()
        window = self.tenant.subscription_due_soon_days
        return today <= self.subscription_due_date <= today + datetime.timedelta(days=window)

    @property
    def last_appointment_date(self):
        """Data do último atendimento concluído — import local pra evitar
        importar `apps.scheduling` no carregamento do app (mesmo padrão de
        `apps/clients/services.py`)."""
        from apps.scheduling.models import AppointmentStatus

        last = (
            self.appointments.filter(status=AppointmentStatus.COMPLETED)
            .order_by("-date")
            .first()
        )
        return last.date if last else None

    @property
    def is_inactive(self):
        """Sem atendimento concluído há `Tenant.client_inactive_days` dias
        (Configurações) — conta a partir do cadastro se o cliente nunca
        voltou."""
        reference_date = self.last_appointment_date or self.created_at.date()
        days_since = (datetime.date.today() - reference_date).days
        return days_since >= self.tenant.client_inactive_days

    @property
    def net_balance(self):
        """Saldo líquido crédito − débito, só para exibição (ex.: coluna
        "Saldo" na lista de Clientes) — positivo = cliente tem crédito a
        favor, negativo = cliente deve. Não é armazenado nem usado em
        cálculo financeiro nenhum; `credit_balance`/`debt_balance` continuam
        sendo os saldos de verdade, cada um só alterado pelas próprias
        funções de `apps/clients/services.py`."""
        return self.credit_balance - self.debt_balance


class Package(TenantModel):
    """Pacote de mensalidade (decisão do usuário em 2026-08-04) — define quais
    serviços um cliente mensalista já tem incluídos (sem cobrança extra no
    Caixa quando agenda um deles) e se o funcionário ainda ganha comissão
    nesses atendimentos. Cliente é vinculado a UM pacote por vez (ver
    `Client.package`) via `apps.clients.services.assign_package_to_client`,
    que gera a cobrança do pacote no Caixa."""

    name = models.CharField("nome", max_length=120)
    description = models.CharField("descrição", max_length=255, blank=True)
    price = models.DecimalField(
        "valor mensal", max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    services = models.ManyToManyField(
        "services.Service", related_name="packages", blank=True,
        verbose_name="serviços inclusos",
    )
    generates_commission = models.BooleanField(
        "gera comissão pro funcionário", default=True,
        help_text="Marcado: mesmo o cliente não pagando por esse atendimento específico "
        "(já incluso na mensalidade), o funcionário ainda recebe a comissão normal, "
        "calculada sobre o valor de tabela do serviço. Desmarcado: nenhuma comissão nesses "
        "atendimentos.",
    )
    is_active = models.BooleanField("ativo", default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "pacote"
        verbose_name_plural = "pacotes"
        ordering = ["name"]

    def __str__(self):
        return self.name


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


class ClientDebtTransaction(TenantModel):
    """Movimentação da carteira de DÉBITO do cliente (fiado) — cada uma
    recalcula `Client.debt_balance`. Espelha `ClientCreditTransaction`, mas
    com a polaridade invertida:

    IN = débito lançado (cliente passou a dever mais) — acontece quando uma
    comanda fecha sem cobrir o total (`apps.scheduling.services.
    complete_appointment`, parâmetro `debt_amount`). NÃO gera
    `CashTransaction`: o dinheiro correspondente não entrou no caixa.

    OUT = débito quitado ou perdoado/corrigido (cliente passou a dever
    menos). Quitação de verdade (`settle_client_debt`) SEMPRE gera uma
    `CashTransaction` real (é o momento em que a receita é finalmente
    reconhecida — regime de caixa); ajuste manual sem dinheiro envolvido
    (`write_off_client_debt`) não gera.
    """

    client = models.ForeignKey(
        Client,
        # PROTECT: histórico da carteira é auditoria financeira — nunca some
        # junto com o cliente (a anonimização LGPD preserva o ledger).
        on_delete=models.PROTECT,
        related_name="debt_transactions",
    )
    # Reaproveita o enum de apps.finance (IN=débito lançado, OUT=quitado).
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
        related_name="debt_transactions",
    )
    related_cash_transaction = models.ForeignKey(
        "finance.CashTransaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="debt_transactions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "movimentação de débito"
        verbose_name_plural = "movimentações de débito"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "client", "created_at"])]

    def __str__(self):
        return f"{self.type} — {self.client} — R$ {self.amount}"
