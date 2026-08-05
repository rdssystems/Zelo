from django.conf import settings
from django.db import models

from apps.tenants.models import TenantModel


class Announcement(models.Model):
    """Aviso de atualização do app, criado pelo superadmin em /plataforma/ e
    visto por TODOS os tenant_admins da plataforma (broadcast, sem alvo por
    tenant — pedido explícito do usuário: "para todos os tenants")."""

    title = models.CharField("título", max_length=200)
    message = models.TextField("mensagem")
    is_active = models.BooleanField("ativo", default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "aviso"
        verbose_name_plural = "avisos"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class AnnouncementRead(models.Model):
    """Marca que um tenant_admin específico já leu um aviso (leitura é por
    usuário, não por tenant — cada admin dispensa a sua própria)."""

    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="announcement_reads"
    )
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "leitura de aviso"
        verbose_name_plural = "leituras de aviso"
        constraints = [
            models.UniqueConstraint(
                fields=["announcement", "user"], name="unique_announcement_read_per_user"
            ),
        ]

    def __str__(self):
        return f"{self.user} leu '{self.announcement}'"


class TenantNotificationKind(models.TextChoices):
    """`APPOINTMENT_CANCELED_BY_CLIENT` e `APPOINTMENT_CREATED_BY_CLIENT`
    (2026-08-06) são geradas hoje. As demais permanecem aqui como roadmap já
    modelado (reagendamento etc.), ver 01-REQUISITOS.md."""

    APPOINTMENT_CANCELED_BY_CLIENT = "appointment_canceled_by_client", "Cliente cancelou agendamento"
    APPOINTMENT_CREATED_BY_CLIENT = "appointment_created_by_client", "Cliente criou novo agendamento"
    BIRTHDAY_ALERT = "birthday_alert", "Aniversariante(s) do dia"


class TenantNotification(TenantModel):
    """Alerta operacional de UM salão (diferente de `Announcement`, que é
    aviso da plataforma pra TODOS os tenants) — nasce de um evento dentro do
    próprio tenant. Leitura é direto no registro (`is_read`/`read_at`), sem
    tabela de join por usuário como `AnnouncementRead`: aqui só o(s)
    tenant_admin(s) do próprio salão veem, não precisa rastrear por usuário."""

    kind = models.CharField("tipo", max_length=40, choices=TenantNotificationKind.choices)
    title = models.CharField("título", max_length=200)
    message = models.TextField("mensagem")
    appointment = models.ForeignKey(
        "scheduling.Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    is_read = models.BooleanField("lida", default=False)
    read_at = models.DateTimeField("lida em", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "notificação"
        verbose_name_plural = "notificações"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
