import uuid

from django.core.exceptions import ValidationError
from django.db import models

# A Etapa 5 passou a servir a página pública em `/<slug>/` — um tenant com
# slug igual a um desses prefixos quebraria as rotas fixas do sistema
# (config/urls.py). Reforçar em Etapa 8 na validação do form de configurações.
RESERVED_TENANT_SLUGS = {
    "painel", "superadmin", "plataforma", "api", "healthz", "static", "media",
    "cadastrar", "termos", "privacidade", "accounts",
}


def validate_slug_not_reserved(value):
    if value in RESERVED_TENANT_SLUGS:
        raise ValidationError(
            f"O slug '{value}' é reservado pelo sistema e não pode ser usado."
        )


def _check_digit(digits, weights):
    total = sum(d * w for d, w in zip(digits, weights))
    rest = total % 11
    return 0 if rest < 2 else 11 - rest


def validate_cpf_cnpj(value):
    """CPF (11 dígitos) ou CNPJ (14 dígitos), com verificação real de dígito
    verificador — exigido pelo Asaas (`cpfCnpj`) pra criar o cliente da
    assinatura (`apps/billing/asaas_client.py::create_customer`)."""
    digits_str = "".join(ch for ch in value if ch.isdigit())
    digits = [int(ch) for ch in digits_str]

    if len(digits) == 11:
        if digits_str == digits_str[0] * 11:
            raise ValidationError("CPF inválido.")
        d1 = _check_digit(digits[:9], range(10, 1, -1))
        d2 = _check_digit(digits[:9] + [d1], range(11, 1, -1))
        if [d1, d2] != digits[9:]:
            raise ValidationError("CPF inválido.")
    elif len(digits) == 14:
        if digits_str == digits_str[0] * 14:
            raise ValidationError("CNPJ inválido.")
        d1 = _check_digit(digits[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
        d2 = _check_digit(digits[:12] + [d1], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
        if [d1, d2] != digits[12:]:
            raise ValidationError("CNPJ inválido.")
    else:
        raise ValidationError("Informe um CPF (11 dígitos) ou CNPJ (14 dígitos) válido.")


class TenantTheme(models.TextChoices):
    """Tema visual do tenant (decisão do usuário em 2026-08-01) — escolhido no
    cadastro (dois botões) e editável depois em Configurações. Só afeta
    aparência (paleta/tipografia via `templates/_theme_tailwind_config.html`
    e `templates/_theme_fonts.html`) — nenhuma regra de negócio muda entre
    os dois."""

    SALAO = "salao", "Salão de beleza"
    BARBEARIA = "barbearia", "Barbearia"


class Tenant(models.Model):
    """Um salão de estética (cliente da plataforma)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("nome fantasia", max_length=120)
    slug = models.SlugField(
        "slug", max_length=60, unique=True, validators=[validate_slug_not_reserved]
    )
    whatsapp = models.CharField("WhatsApp", max_length=20, blank=True)
    document = models.CharField(
        "CPF/CNPJ", max_length=18, blank=True, validators=[validate_cpf_cnpj],
        help_text="Exigido só na hora de assinar um plano pago (Asaas precisa pra criar o cliente).",
    )
    address = models.CharField("endereço", max_length=255, blank=True)
    description = models.TextField("descrição", blank=True)
    logo = models.ImageField(upload_to="tenants/logos/", blank=True, null=True)
    cover_image = models.ImageField(upload_to="tenants/covers/", blank=True, null=True)
    background_image = models.ImageField(
        upload_to="tenants/backgrounds/", blank=True, null=True
    )
    theme = models.CharField(
        "tema visual", max_length=20, choices=TenantTheme.choices, default=TenantTheme.SALAO,
        help_text="Escolhido no cadastro, editável em Configurações — muda a paleta e "
        "tipografia do app público e do painel.",
    )
    # Limiares configuráveis em Configurações (decisão do usuário em
    # 2026-07-31) — usados por `apps.clients.models.Client` pra marcar
    # mensalista "a vencer" e cliente "inativo" na lista/campanha de WhatsApp.
    subscription_due_soon_days = models.PositiveSmallIntegerField(
        "aviso de mensalidade a vencer (dias)", default=7,
        help_text="Mensalista aparece como \"vence em breve\" quando faltar esse tanto de "
        "dias (ou menos) pro vencimento.",
    )
    client_inactive_days = models.PositiveSmallIntegerField(
        "cliente inativo após (dias)", default=60,
        help_text="Sem nenhum atendimento concluído há esse tanto de dias (contado do "
        "cadastro se o cliente nunca voltou), aparece marcado como inativo na lista.",
    )
    whatsapp_cancel_redirect_enabled = models.BooleanField(
        "redirecionar cliente pro WhatsApp ao cancelar", default=True,
        help_text="Ao cancelar um agendamento na página pública, abre o WhatsApp do salão "
        "com um aviso pronto (editável) pro cliente só clicar em enviar. Exige WhatsApp "
        "cadastrado acima.",
    )
    auto_confirm_appointments = models.BooleanField(
        "confirmar agendamento automaticamente", default=False,
        help_text="Desmarcado (padrão): todo agendamento nasce pendente, precisa o salão "
        "confirmar na Agenda — a página do cliente mostra \"Agendamento enviado\" até lá. "
        "Marcado: agendamento já nasce confirmado, sem esperar o salão.",
    )
    is_active = models.BooleanField("ativo", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "tenant"
        verbose_name_plural = "tenants"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def whatsapp_wa_me_number(self):
        """Só dígitos, com DDI — usado pra montar link `wa.me` (ex.: aviso de
        cancelamento do cliente). `None` sem WhatsApp cadastrado. Mesma ideia
        de `apps.clients.models.Client.whatsapp_url`, mas o campo aqui não é
        normalizado na entrada (`Tenant.whatsapp` aceita qualquer formato),
        então limpa a formatação e garante o `55` na frente aqui mesmo."""
        digits = "".join(ch for ch in self.whatsapp if ch.isdigit())
        if not digits:
            return None
        if not digits.startswith("55"):
            digits = f"55{digits}"
        return digits


class TenantQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        return self.filter(tenant=tenant)


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """Manager padrão de toda model tenant-aware.

    Uso obrigatório nas views/services: `Model.objects.for_tenant(request.tenant)`.
    Nunca usar `.all()` sem filtro de tenant em código de painel/página pública.
    """


class TenantModel(models.Model):
    """Base abstrata de toda model tenant-aware (regra #1 do CLAUDE.md).

    Toda query sobre subclasses deve ser filtrada por tenant — o caminho
    padrão é `objects.for_tenant(tenant)`.
    """

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
    )

    objects = TenantManager()

    class Meta:
        abstract = True


class Weekday(models.IntegerChoices):
    """Mesma convenção de `apps.employees.models.Weekday` (0=Segunda..6=Domingo,
    igual `date.weekday()`) — duplicada aqui (não importada de lá) porque
    `apps.employees.models` já importa `apps.tenants.models`, e importar de
    volta criaria um import circular."""

    MONDAY = 0, "Segunda-feira"
    TUESDAY = 1, "Terça-feira"
    WEDNESDAY = 2, "Quarta-feira"
    THURSDAY = 3, "Quinta-feira"
    FRIDAY = 4, "Sexta-feira"
    SATURDAY = 5, "Sábado"
    SUNDAY = 6, "Domingo"


class TenantBusinessHours(TenantModel):
    """Horário de funcionamento do salão, por dia da semana (RF26) — exibido
    na página pública. Puramente informativo: não valida agendamento (quem
    trava disponibilidade de horário é `WorkingHours` do funcionário, ver
    02-ARQUITETURA.md §6)."""

    weekday = models.PositiveSmallIntegerField("dia da semana", choices=Weekday.choices)
    is_closed = models.BooleanField("fechado", default=False)
    start_time = models.TimeField("abre", null=True, blank=True)
    end_time = models.TimeField("fecha", null=True, blank=True)

    class Meta:
        verbose_name = "horário de funcionamento"
        verbose_name_plural = "horários de funcionamento"
        ordering = ["weekday"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "weekday"], name="unique_business_hours_weekday_per_tenant"
            ),
        ]

    def __str__(self):
        if self.is_closed:
            return f"{self.get_weekday_display()} — fechado"
        return f"{self.get_weekday_display()} {self.start_time:%H:%M}-{self.end_time:%H:%M}"
