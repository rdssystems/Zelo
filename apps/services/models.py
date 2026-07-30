from django.core.validators import MinValueValidator
from django.db import models

from apps.tenants.models import TenantModel


class Service(TenantModel):
    """Serviço oferecido pelo salão (RF13)."""

    name = models.CharField("nome", max_length=120)
    description = models.TextField("descrição", blank=True)
    duration_minutes = models.PositiveIntegerField(
        "duração (minutos)", validators=[MinValueValidator(1)]
    )
    price = models.DecimalField(
        "preço",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    is_active = models.BooleanField("ativo", default=True)

    class Meta:
        verbose_name = "serviço"
        verbose_name_plural = "serviços"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="unique_service_name_per_tenant"
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
        ]

    def __str__(self):
        return self.name
