from django.conf import settings
from django.db import models

from apps.tenants.models import TenantModel


class AppointmentStatus(models.TextChoices):
    PENDING = "pending", "Pendente"
    CONFIRMED = "confirmed", "Confirmado"
    IN_PROGRESS = "in_progress", "Em Atendimento"
    COMPLETED = "completed", "Concluído"
    CANCELED = "canceled", "Cancelado"
    NO_SHOW = "no_show", "Não compareceu"


# Status que ocupam a agenda do funcionário (RF05, RF15) — usados pelo motor
# de disponibilidade em availability.py. Concluído/cancelado/no-show liberam
# o horário.
BLOCKING_STATUSES = [
    AppointmentStatus.PENDING,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.IN_PROGRESS,
]


class Appointment(TenantModel):
    """Agendamento (RF15). Essencial para o motor de disponibilidade — os
    campos de conclusão atômica (comissão/caixa/estoque) entram na Etapa 7."""

    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    service = models.ForeignKey(
        "services.Service",
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    date = models.DateField("data")
    start_time = models.TimeField("início")
    end_time = models.TimeField("fim")
    status = models.CharField(
        "status",
        max_length=20,
        choices=AppointmentStatus.choices,
        default=AppointmentStatus.PENDING,
    )
    price_at_booking = models.DecimalField(
        "preço no momento do agendamento", max_digits=10, decimal_places=2
    )
    # Snapshot de qual pacote de mensalidade cobria este serviço no momento
    # do agendamento (decisão do usuário em 2026-08-04) — preenchido em
    # `apps.scheduling.services.create_appointment` quando o cliente é
    # mensalista de um pacote que inclui o serviço. `price_at_booking`
    # continua o valor de TABELA do serviço mesmo assim (nunca zerado) —
    # é a base de cálculo de comissão; quem decide não cobrar do cliente no
    # Caixa é este campo, não o preço (ver `complete_appointment`).
    package = models.ForeignKey(
        "clients.Package",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="covered_appointments",
    )
    notes = models.TextField("observações", blank=True)
    canceled_by_client = models.BooleanField(
        "cancelado pelo cliente", default=False,
        help_text="True quando o próprio cliente cancelou pela página pública (RF06) — "
        "distingue de cancelamento feito pelo admin do salão na Agenda, pra exibir o aviso "
        "certo (`apps.scheduling.services.cancel_appointment`).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Nulo se criado pelo cliente na página pública.",
    )

    class Meta:
        verbose_name = "agendamento"
        verbose_name_plural = "agendamentos"
        ordering = ["date", "start_time"]
        indexes = [
            models.Index(fields=["tenant", "employee", "date"]),
        ]
        constraints = [
            models.CheckConstraint(
                name="appointment_end_after_start",
                condition=models.Q(end_time__gt=models.F("start_time")),
            ),
            # Defesa contra condição de corrida: dois clientes confirmando o
            # mesmo horário ao mesmo tempo (a checagem de disponibilidade em
            # availability.py não é atômica sozinha). O índice único parcial
            # garante no banco que só um agendamento ATIVO pode existir para
            # este funcionário nesse dia+horário — o segundo INSERT falha e
            # apps/scheduling/services.py converte isso num erro amigável.
            models.UniqueConstraint(
                fields=["employee", "date", "start_time"],
                condition=models.Q(status__in=["pending", "confirmed", "in_progress"]),
                name="unique_active_appointment_per_slot",
            ),
        ]

    def __str__(self):
        return f"{self.client} — {self.employee} — {self.date} {self.start_time:%H:%M}"
