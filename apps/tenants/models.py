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


class Tenant(models.Model):
    """Um salão de estética (cliente da plataforma)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("nome fantasia", max_length=120)
    slug = models.SlugField(
        "slug", max_length=60, unique=True, validators=[validate_slug_not_reserved]
    )
    whatsapp = models.CharField("WhatsApp", max_length=20, blank=True)
    address = models.CharField("endereço", max_length=255, blank=True)
    description = models.TextField("descrição", blank=True)
    logo = models.ImageField(upload_to="tenants/logos/", blank=True, null=True)
    cover_image = models.ImageField(upload_to="tenants/covers/", blank=True, null=True)
    background_image = models.ImageField(
        upload_to="tenants/backgrounds/", blank=True, null=True
    )
    is_active = models.BooleanField("ativo", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "tenant"
        verbose_name_plural = "tenants"
        ordering = ["name"]

    def __str__(self):
        return self.name


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
