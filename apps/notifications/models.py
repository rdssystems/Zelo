from django.conf import settings
from django.db import models


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
