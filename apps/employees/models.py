from django.conf import settings
from django.db import models

from apps.tenants.models import TenantModel


class CommissionType(models.TextChoices):
    PERCENTAGE = "percentage", "Percentual (%)"
    FIXED = "fixed", "Valor fixo (R$)"


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Segunda-feira"
    TUESDAY = 1, "Terça-feira"
    WEDNESDAY = 2, "Quarta-feira"
    THURSDAY = 3, "Quinta-feira"
    FRIDAY = 4, "Sexta-feira"
    SATURDAY = 5, "Sábado"
    SUNDAY = 6, "Domingo"


class Employee(TenantModel):
    """Profissional do salão (RF07). Sempre tem um User 1:1 para login (RF08)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="employee_profile",
    )
    full_name = models.CharField("nome completo", max_length=120)
    photo = models.ImageField(upload_to="employees/photos/", blank=True, null=True)
    bio = models.TextField("bio", blank=True)
    phone = models.CharField("telefone", max_length=20, blank=True)
    default_commission_type = models.CharField(
        "tipo de comissão padrão",
        max_length=20,
        choices=CommissionType.choices,
        default=CommissionType.PERCENTAGE,
    )
    default_commission_value = models.DecimalField(
        "valor de comissão padrão", max_digits=10, decimal_places=2
    )
    is_active = models.BooleanField("ativo", default=True)

    class Meta:
        verbose_name = "funcionário"
        verbose_name_plural = "funcionários"
        ordering = ["full_name"]
        indexes = [models.Index(fields=["tenant", "is_active"])]

    def __str__(self):
        return self.full_name


class WorkingHours(TenantModel):
    """Jornada semanal do funcionário (RF10)."""

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="working_hours"
    )
    weekday = models.PositiveSmallIntegerField(
        "dia da semana", choices=Weekday.choices
    )
    start_time = models.TimeField("início")
    end_time = models.TimeField("fim")
    is_active = models.BooleanField("ativo", default=True)

    class Meta:
        verbose_name = "jornada"
        verbose_name_plural = "jornadas"
        ordering = ["weekday", "start_time"]
        constraints = [
            models.CheckConstraint(
                name="workinghours_end_after_start",
                condition=models.Q(end_time__gt=models.F("start_time")),
            ),
        ]

    def __str__(self):
        return (
            f"{self.employee} — {self.get_weekday_display()} "
            f"{self.start_time:%H:%M}-{self.end_time:%H:%M}"
        )


class ScheduleException(TenantModel):
    """Folga/bloqueio pontual (RF10). start/end nulos = dia inteiro bloqueado."""

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="schedule_exceptions"
    )
    date = models.DateField("data")
    start_time = models.TimeField("início", null=True, blank=True)
    end_time = models.TimeField("fim", null=True, blank=True)
    reason = models.CharField("motivo", max_length=120, blank=True)

    class Meta:
        verbose_name = "exceção de agenda"
        verbose_name_plural = "exceções de agenda"
        ordering = ["date", "start_time"]
        constraints = [
            # ou dia inteiro (ambos nulos), ou intervalo válido (ambos definidos)
            models.CheckConstraint(
                name="scheduleexception_full_day_or_valid_range",
                condition=(
                    models.Q(start_time__isnull=True, end_time__isnull=True)
                    | models.Q(
                        start_time__isnull=False,
                        end_time__isnull=False,
                        end_time__gt=models.F("start_time"),
                    )
                ),
            ),
        ]

    def __str__(self):
        return f"{self.employee} — {self.date}"


class EmployeeService(TenantModel):
    """Vínculo funcionário↔serviço (RF09).

    Se `commission_type`/`commission_value` estiverem definidos, sobrescrevem a
    comissão padrão do funcionário PARA ESTE SERVIÇO. Prioridade documentada em
    `apps/employees/services.py::get_commission_config` (regra 4 do CLAUDE.md):
    override do vínculo > padrão do funcionário.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="service_links"
    )
    service = models.ForeignKey(
        "services.Service", on_delete=models.CASCADE, related_name="employee_links"
    )
    commission_type = models.CharField(
        "tipo de comissão (override)",
        max_length=20,
        choices=CommissionType.choices,
        null=True,
        blank=True,
    )
    commission_value = models.DecimalField(
        "valor de comissão (override)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "vínculo funcionário-serviço"
        verbose_name_plural = "vínculos funcionário-serviço"
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "service"],
                name="unique_employee_service",
            ),
            # override é tudo-ou-nada: tipo e valor juntos, ou nenhum
            models.CheckConstraint(
                name="employeeservice_override_all_or_nothing",
                condition=(
                    models.Q(
                        commission_type__isnull=True, commission_value__isnull=True
                    )
                    | models.Q(
                        commission_type__isnull=False,
                        commission_value__isnull=False,
                    )
                ),
            ),
        ]

    def __str__(self):
        return f"{self.employee} ↔ {self.service}"
