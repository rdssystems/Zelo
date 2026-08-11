from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.tenants.models import TenantModel


class MovementType(models.TextChoices):
    IN = "in", "Entrada"
    OUT = "out", "Saída"


class MovementReason(models.TextChoices):
    PURCHASE = "purchase", "Compra"
    SALE = "sale", "Venda"
    SERVICE_USE = "service_use", "Uso em serviço"
    # RF48 — consumo automático da receita (ficha técnica) de um serviço,
    # diferente de SERVICE_USE (venda casada manual, cobrada do cliente):
    # este NUNCA gera CashTransaction. Mantido separado pra não misturar
    # histórico cobrado com não-cobrado nos relatórios de estoque.
    RECIPE_USE = "recipe_use", "Insumo de receita (automático)"
    ADJUSTMENT = "adjustment", "Ajuste"
    LOSS = "loss", "Perda"


class ProductUnit(models.TextChoices):
    UNIT = "un", "Unidade (un)"
    ML = "ml", "Mililitro (ml)"
    LITER = "l", "Litro (L)"
    GRAM = "g", "Grama (g)"
    KG = "kg", "Quilograma (kg)"
    BOX = "cx", "Caixa (cx)"
    PAIR = "par", "Par"


# Unidades vendidas em quantidade inteira (não dá pra vender "2,04" unidades
# ou "1,5" par) — o resto (ml/L/g/kg) aceita fração. Usado em qualquer lugar
# que valide quantidade de produto vendido (comanda, venda avulsa).
WHOLE_UNIT_CODES = {ProductUnit.UNIT, ProductUnit.PAIR, ProductUnit.BOX}


class Category(TenantModel):
    """Categoria de produto (organização do catálogo — agrupa produtos no
    modal de venda da comanda e na listagem de Estoque)."""

    name = models.CharField("nome", max_length=80)

    class Meta:
        verbose_name = "categoria"
        verbose_name_plural = "categorias"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="unique_category_name_per_tenant"
            ),
        ]

    def __str__(self):
        return self.name


class Supplier(TenantModel):
    """Fornecedor de produto (RF43) — opcional, tanto como preferido no
    cadastro do produto quanto vinculado a uma compra específica."""

    name = models.CharField("nome", max_length=120)
    contact_name = models.CharField("contato", max_length=120, blank=True)
    phone = models.CharField("telefone", max_length=20, blank=True)
    email = models.EmailField("e-mail", blank=True)
    notes = models.TextField("observações", blank=True)
    is_active = models.BooleanField("ativo", default=True)

    class Meta:
        verbose_name = "fornecedor"
        verbose_name_plural = "fornecedores"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="unique_supplier_name_per_tenant"
            ),
        ]

    def __str__(self):
        return self.name


class Product(TenantModel):
    """Produto de estoque (RF18)."""

    name = models.CharField("nome", max_length=120)
    sku = models.CharField("SKU", max_length=40, blank=True)
    category = models.ForeignKey(
        Category,
        # PROTECT: categoria com produtos vinculados não pode ser excluída
        # sem reatribuir os produtos primeiro (decisão do usuário).
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="products",
    )
    supplier = models.ForeignKey(
        Supplier,
        # SET_NULL: fornecedor é só um dado informativo/preferido — excluir
        # ou desativar um fornecedor nunca pode travar o produto (RF43).
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    unit = models.CharField(
        "unidade", max_length=10, choices=ProductUnit.choices, default=ProductUnit.UNIT
    )
    cost_price = models.DecimalField(
        "preço de custo", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    sale_price = models.DecimalField(
        "preço de venda", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    # Derivado — NUNCA editar direto (CLAUDE.md regra 2). Só muda via
    # apps/inventory/services.py::register_stock_movement.
    current_stock = models.DecimalField(
        "estoque atual", max_digits=10, decimal_places=2, default=0
    )
    min_stock_alert = models.DecimalField(
        "estoque mínimo", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    is_active = models.BooleanField("ativo", default=True)
    # RF44 — opt-in: nem todo produto vence (ex. toalha). Quando True, toda
    # entrada de compra abre um ProductBatch e toda saída desconta por FEFO
    # (lote mais próximo do vencimento primeiro) — ver register_stock_movement.
    tracks_batches = models.BooleanField("controla lote/validade", default=False)
    # RF48 — False = insumo de uso interno (ex. tinta, luva): some dos
    # pickers de venda ("Vender produto"/"Nova Venda"), mas continua com
    # toda a engine de Estoque (fornecedor, lote/validade, custo médio,
    # alerta de estoque baixo) e pode ser usado numa receita de serviço
    # (apps.services.models.ServiceProduct).
    is_for_sale = models.BooleanField("vender ao cliente", default=True)

    class Meta:
        verbose_name = "produto"
        verbose_name_plural = "produtos"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="unique_product_name_per_tenant"
            ),
        ]
        indexes = [models.Index(fields=["tenant", "is_active"])]

    def __str__(self):
        return self.name

    @property
    def is_low_stock(self):
        return self.current_stock <= self.min_stock_alert

    @property
    def has_purchase_history(self):
        """RF45 — desde a primeira compra registrada, `cost_price` só muda
        via recálculo automático de custo médio (`register_stock_movement`),
        nunca editado manualmente (decisão do usuário)."""
        return self.movements.filter(reason=MovementReason.PURCHASE).exists()


class StockMovement(TenantModel):
    """Movimentação de estoque (RF19) — cada uma recalcula `Product.current_stock`."""

    product = models.ForeignKey(
        Product,
        # PROTECT: histórico de movimentação é auditoria financeira/estoque
        # (RNF05) — nunca pode sumir junto com o produto.
        on_delete=models.PROTECT,
        related_name="movements",
    )
    type = models.CharField("tipo", max_length=10, choices=MovementType.choices)
    quantity = models.DecimalField(
        "quantidade",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    unit_price = models.DecimalField(
        "preço unitário", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    total_value = models.DecimalField("valor total", max_digits=10, decimal_places=2)
    reason = models.CharField("motivo", max_length=20, choices=MovementReason.choices)
    # Fornecedor DESTA compra específica (RF43) — pode ser diferente do
    # fornecedor preferido cadastrado no produto (Product.supplier).
    supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements"
    )
    related_appointment = models.ForeignKey(
        "scheduling.Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "movimentação de estoque"
        verbose_name_plural = "movimentações de estoque"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "product", "created_at"])]

    def __str__(self):
        return f"{self.get_type_display()} — {self.product} ({self.quantity})"


class ProductBatch(TenantModel):
    """Lote de compra de um produto que controla validade (RF44) — aberto a
    cada `StockMovement` de compra (entrada). `quantity_remaining` é
    **derivado**: abatido conforme saídas consomem deste lote (FEFO), nunca
    editado direto (mesma regra 2 do CLAUDE.md de `Product.current_stock`)."""

    product = models.ForeignKey(
        Product,
        # PROTECT: histórico de lote é auditoria de estoque (RNF05) — nunca
        # some junto com o produto.
        on_delete=models.PROTECT,
        related_name="batches",
    )
    batch_number = models.CharField("número do lote", max_length=60, blank=True)
    expiry_date = models.DateField("validade")
    quantity_received = models.DecimalField(
        "quantidade recebida", max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    quantity_remaining = models.DecimalField(
        "quantidade restante", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="batches"
    )
    unit_cost = models.DecimalField(
        "custo unitário da compra", max_digits=10, decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    received_at = models.DateTimeField("recebido em", auto_now_add=True)

    class Meta:
        verbose_name = "lote de produto"
        verbose_name_plural = "lotes de produto"
        ordering = ["expiry_date"]
        indexes = [models.Index(fields=["tenant", "product", "expiry_date"])]

    def __str__(self):
        label = self.batch_number or f"lote #{self.pk}"
        return f"{self.product} — {label} (vence {self.expiry_date})"

    @property
    def is_depleted(self):
        return self.quantity_remaining <= 0


class StockMovementBatch(TenantModel):
    """Rastro de QUANTO uma saída consumiu de cada lote (RF44) — uma saída
    pode esgotar um lote e continuar no próximo mais próximo do vencimento
    (FEFO), por isso é uma tabela à parte e não uma FK direta em
    `StockMovement`."""

    movement = models.ForeignKey(
        StockMovement, on_delete=models.CASCADE, related_name="batch_consumptions"
    )
    batch = models.ForeignKey(
        ProductBatch,
        # PROTECT: consumo de lote é auditoria — nunca some junto com o lote.
        on_delete=models.PROTECT,
        related_name="consumptions",
    )
    quantity = models.DecimalField(
        "quantidade consumida", max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    class Meta:
        verbose_name = "consumo de lote"
        verbose_name_plural = "consumos de lote"

    def __str__(self):
        return f"{self.movement} — {self.quantity} de {self.batch}"


class InventoryCountStatus(models.TextChoices):
    IN_PROGRESS = "in_progress", "Em andamento"
    COMPLETED = "completed", "Concluída"


class PhysicalInventoryCount(TenantModel):
    """Contagem de inventário físico (RF46) — ao abrir, congela
    `expected_quantity` de todo produto ativo; ao fechar, toda divergência
    vira um `StockMovement` automático (`reason=adjustment`)."""

    status = models.CharField(
        "status", max_length=20, choices=InventoryCountStatus.choices,
        default=InventoryCountStatus.IN_PROGRESS,
    )
    started_at = models.DateTimeField("iniciada em", auto_now_add=True)
    completed_at = models.DateTimeField("concluída em", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    notes = models.TextField("observações", blank=True)

    class Meta:
        verbose_name = "contagem de inventário"
        verbose_name_plural = "contagens de inventário"
        ordering = ["-started_at"]

    def __str__(self):
        return f"Contagem #{self.pk} — {self.get_status_display()}"


class PhysicalInventoryCountItem(TenantModel):
    """1 linha por produto ativo, congelada no início da contagem."""

    count = models.ForeignKey(
        PhysicalInventoryCount, on_delete=models.CASCADE, related_name="items"
    )
    product = models.ForeignKey(
        Product,
        # PROTECT: histórico de contagem é auditoria de estoque (RNF05).
        on_delete=models.PROTECT,
        related_name="inventory_count_items",
    )
    expected_quantity = models.DecimalField(
        "quantidade esperada", max_digits=10, decimal_places=2
    )
    counted_quantity = models.DecimalField(
        "quantidade contada", max_digits=10, decimal_places=2, null=True, blank=True
    )

    class Meta:
        verbose_name = "item de contagem"
        verbose_name_plural = "itens de contagem"
        ordering = ["product__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["count", "product"], name="unique_product_per_count"
            ),
        ]

    def __str__(self):
        return f"{self.product} — {self.count}"

    @property
    def difference(self):
        if self.counted_quantity is None:
            return None
        return self.counted_quantity - self.expected_quantity
