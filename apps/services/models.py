from decimal import Decimal

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


class ServiceProduct(TenantModel):
    """Ficha técnica do serviço (RF48) — insumo consumido automaticamente do
    estoque toda vez que um atendimento deste serviço é concluído, sem
    cobrar o cliente por isso (diferente da venda casada manual escolhida
    no fechamento da comanda). Ver `apps.scheduling.services.
    complete_appointment`."""

    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="recipe_items"
    )
    product = models.ForeignKey(
        "inventory.Product",
        # PROTECT: mesmo padrão de todo FK de Product ligado a
        # histórico/uso (StockMovement.product, ComandaProductItem.product)
        # — não dá pra excluir um produto que ainda está numa receita.
        on_delete=models.PROTECT,
        related_name="service_recipe_links",
    )
    quantity = models.DecimalField(
        "quantidade",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    class Meta:
        verbose_name = "insumo do serviço"
        verbose_name_plural = "insumos do serviço"
        ordering = ["product__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["service", "product"], name="unique_service_product_recipe"
            ),
        ]

    def __str__(self):
        return f"{self.service.name} — {self.product.name} ({self.quantity})"

    @property
    def is_whole_unit(self):
        from apps.inventory.models import WHOLE_UNIT_CODES

        return self.product.unit in WHOLE_UNIT_CODES
