"""Operações de domínio de estoque (RF18-RF20, regra 2 do CLAUDE.md).

`Product.current_stock` NUNCA é editado diretamente — toda mudança passa por
`register_stock_movement`, que cria o `StockMovement` e recalcula o estoque
de forma atômica.
"""

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from .models import (
    Category,
    InventoryCountStatus,
    MovementReason,
    MovementType,
    PhysicalInventoryCount,
    PhysicalInventoryCountItem,
    Product,
    ProductBatch,
    StockMovement,
    StockMovementBatch,
    Supplier,
)


def _validate_product(name, cost_price, sale_price, min_stock_alert):
    if not str(name).strip():
        raise ValidationError({"name": "O nome do produto é obrigatório."})
    if Decimal(cost_price) < 0:
        raise ValidationError({"cost_price": "O preço de custo não pode ser negativo."})
    if Decimal(sale_price) < 0:
        raise ValidationError({"sale_price": "O preço de venda não pode ser negativo."})
    if Decimal(min_stock_alert) < 0:
        raise ValidationError(
            {"min_stock_alert": "O estoque mínimo não pode ser negativo."}
        )


def create_product(
    *, tenant, name, unit, cost_price, sale_price, min_stock_alert,
    sku="", category=None, supplier=None, tracks_batches=False,
):
    """`category` é opcional aqui (a obrigatoriedade é imposta no
    formulário/serializer do painel — muitos testes de outros apps criam
    produto direto por aqui sem categoria)."""
    _validate_product(name, cost_price, sale_price, min_stock_alert)
    return Product.objects.create(
        tenant=tenant,
        name=str(name).strip(),
        sku=(sku or "").strip(),
        category=category,
        supplier=supplier,
        unit=str(unit).strip(),
        cost_price=cost_price,
        sale_price=sale_price,
        min_stock_alert=min_stock_alert,
        tracks_batches=bool(tracks_batches),
        current_stock=Decimal("0"),
    )


def update_product(
    product, *, name, unit, cost_price, sale_price, min_stock_alert,
    sku="", category=None, supplier=None, tracks_batches=False,
):
    if product.has_purchase_history:
        # RF45 — trava de custo médio: depois da 1ª compra, cost_price só
        # muda via recálculo automático (register_stock_movement) — ignora
        # silenciosamente qualquer valor manual recebido aqui.
        cost_price = product.cost_price
    _validate_product(name, cost_price, sale_price, min_stock_alert)
    product.name = str(name).strip()
    product.sku = (sku or "").strip()
    product.category = category
    product.supplier = supplier
    product.unit = str(unit).strip()
    product.cost_price = cost_price
    product.sale_price = sale_price
    product.min_stock_alert = min_stock_alert
    product.tracks_batches = bool(tracks_batches)
    product.save(
        update_fields=[
            "name", "sku", "category", "supplier", "unit",
            "cost_price", "sale_price", "min_stock_alert", "tracks_batches",
        ]
    )
    return product


def _validate_category(name):
    if not str(name).strip():
        raise ValidationError({"name": "O nome da categoria é obrigatório."})


def create_category(*, tenant, name):
    _validate_category(name)
    return Category.objects.create(tenant=tenant, name=str(name).strip())


def update_category(category, *, name):
    _validate_category(name)
    category.name = str(name).strip()
    category.save(update_fields=["name"])
    return category


def delete_category(category):
    """Exclusão bloqueada enquanto houver produtos vinculados (decisão do
    usuário — reatribuir os produtos antes de excluir)."""
    try:
        category.delete()
    except ProtectedError:
        raise ValidationError(
            "Não é possível excluir: existem produtos vinculados a esta categoria."
        )


def _validate_supplier(name):
    if not str(name).strip():
        raise ValidationError({"name": "O nome do fornecedor é obrigatório."})


def create_supplier(*, tenant, name, contact_name="", phone="", email="", notes=""):
    _validate_supplier(name)
    return Supplier.objects.create(
        tenant=tenant,
        name=str(name).strip(),
        contact_name=(contact_name or "").strip(),
        phone=(phone or "").strip(),
        email=(email or "").strip(),
        notes=(notes or "").strip(),
    )


def update_supplier(supplier, *, name, contact_name="", phone="", email="", notes=""):
    _validate_supplier(name)
    supplier.name = str(name).strip()
    supplier.contact_name = (contact_name or "").strip()
    supplier.phone = (phone or "").strip()
    supplier.email = (email or "").strip()
    supplier.notes = (notes or "").strip()
    supplier.save(update_fields=["name", "contact_name", "phone", "email", "notes"])
    return supplier


def set_supplier_active(supplier, is_active):
    supplier.is_active = bool(is_active)
    supplier.save(update_fields=["is_active"])
    return supplier


def set_product_active(product, is_active):
    product.is_active = bool(is_active)
    product.save(update_fields=["is_active"])
    return product


def delete_product(product):
    """Exclusão definitiva — o modal de confirmação do painel é a barreira
    contra clique acidental (não exige mais desativar antes; pedido do
    usuário em 2026-07-29). A proteção real contra perda de histórico é o
    PROTECT abaixo."""
    try:
        product.delete()
    except ProtectedError:
        # StockMovement.product é PROTECT — ver apps/inventory/models.py
        raise ValidationError(
            "Não é possível excluir: existem movimentações de estoque vinculadas a este "
            "produto. Desative-o para que saia de uso, mantendo o histórico."
        )


def _open_batch(*, tenant, product, quantity, unit_cost, supplier, batch_number, expiry_date):
    if not expiry_date:
        raise ValidationError(
            {"expiry_date": f"Informe a validade — \"{product.name}\" controla lote."}
        )
    return ProductBatch.objects.create(
        tenant=tenant,
        product=product,
        batch_number=(batch_number or "").strip(),
        expiry_date=expiry_date,
        quantity_received=quantity,
        quantity_remaining=quantity,
        supplier=supplier,
        unit_cost=unit_cost,
    )


def _consume_batches_fefo(*, movement, product, quantity):
    """Desconta primeiro o lote mais próximo do vencimento (FEFO) — uma
    única saída pode esgotar vários lotes seguidos. Cada consumo vira um
    `StockMovementBatch` (rastro de auditoria de qual lote foi usado)."""
    remaining = quantity
    batches = ProductBatch.objects.filter(
        tenant=movement.tenant, product=product, quantity_remaining__gt=0
    ).order_by("expiry_date")
    for batch in batches:
        if remaining <= 0:
            break
        used = min(batch.quantity_remaining, remaining)
        batch.quantity_remaining -= used
        batch.save(update_fields=["quantity_remaining"])
        StockMovementBatch.objects.create(
            tenant=movement.tenant, movement=movement, batch=batch, quantity=used
        )
        remaining -= used
    if remaining > 0:
        raise ValidationError(
            f"Estoque insuficiente nos lotes cadastrados de \"{product.name}\" "
            f"(faltam {remaining} {product.unit} — verifique lotes vencidos ou zerados)."
        )


def _recalculate_average_cost(product, *, quantity, unit_price):
    """RF45 — custo médio ponderado: (estoque atual × custo atual + qtd ×
    preço da compra) / (estoque atual + qtd). Só compra (IN + PURCHASE)
    recalcula — ajuste/perda não mexem no custo médio."""
    new_stock = product.current_stock + quantity
    if new_stock <= 0:
        return product.cost_price
    weighted = (
        product.current_stock * product.cost_price + quantity * unit_price
    ) / new_stock
    return weighted.quantize(Decimal("0.01"))


def batches_expiring_soon(tenant, days=30):
    """RF44 — lotes com saldo que vencem dentro de `days` dias (inclui já
    vencidos), pra alerta no painel (mesma ideia do estoque mínimo, RF20)."""
    threshold = datetime.date.today() + datetime.timedelta(days=days)
    return (
        ProductBatch.objects.for_tenant(tenant)
        .filter(quantity_remaining__gt=0, expiry_date__lte=threshold)
        .select_related("product")
        .order_by("expiry_date")
    )


@transaction.atomic
def register_stock_movement(
    *,
    tenant,
    product,
    movement_type,
    quantity,
    unit_price,
    reason,
    created_by,
    related_appointment=None,
    supplier=None,
    batch_number="",
    expiry_date=None,
):
    """Cria o `StockMovement` e recalcula `current_stock` — único caminho
    permitido para alterar o estoque (CLAUDE.md regra 2).

    Se `product.tracks_batches` (RF44): toda entrada de COMPRA abre um novo
    `ProductBatch` (exige `expiry_date`); toda saída desconta por FEFO entre
    os lotes com saldo. Outros tipos de entrada (ex. ajuste) não abrem lote —
    só mexem em `current_stock` normalmente."""
    if product.tenant_id != tenant.id:
        raise ValidationError("Produto não pertence a este tenant.")
    if movement_type not in MovementType.values:
        raise ValidationError("Tipo de movimentação inválido.")
    if reason not in MovementReason.values:
        raise ValidationError("Motivo de movimentação inválido.")

    quantity = Decimal(quantity)
    unit_price = Decimal(unit_price)
    if quantity <= 0:
        raise ValidationError({"quantity": "A quantidade deve ser maior que zero."})
    if unit_price < 0:
        raise ValidationError({"unit_price": "O preço unitário não pode ser negativo."})

    if movement_type == MovementType.OUT and quantity > product.current_stock:
        raise ValidationError(
            f"Estoque insuficiente: disponível {product.current_stock} {product.unit}."
        )

    movement = StockMovement.objects.create(
        tenant=tenant,
        product=product,
        type=movement_type,
        quantity=quantity,
        unit_price=unit_price,
        total_value=quantity * unit_price,
        reason=reason,
        related_appointment=related_appointment,
        supplier=supplier,
        created_by=created_by,
    )

    if product.tracks_batches:
        if movement_type == MovementType.IN and reason == MovementReason.PURCHASE:
            _open_batch(
                tenant=tenant, product=product, quantity=quantity, unit_cost=unit_price,
                supplier=supplier, batch_number=batch_number, expiry_date=expiry_date,
            )
        elif movement_type == MovementType.OUT:
            _consume_batches_fefo(movement=movement, product=product, quantity=quantity)

    update_fields = ["current_stock"]
    if movement_type == MovementType.IN and reason == MovementReason.PURCHASE:
        # RF45 — recalcula ANTES de aplicar o delta (usa o estoque/custo
        # vigentes antes desta compra).
        product.cost_price = _recalculate_average_cost(
            product, quantity=quantity, unit_price=unit_price
        )
        update_fields.append("cost_price")

    delta = quantity if movement_type == MovementType.IN else -quantity
    product.current_stock = product.current_stock + delta
    product.save(update_fields=update_fields)
    return movement


@transaction.atomic
def start_inventory_count(*, tenant, created_by, notes=""):
    """RF46 — abre uma contagem e congela `expected_quantity` (snapshot de
    `current_stock`) de todo produto ativo do tenant."""
    count = PhysicalInventoryCount.objects.create(
        tenant=tenant, created_by=created_by, notes=(notes or "").strip()
    )
    products = Product.objects.for_tenant(tenant).filter(is_active=True)
    PhysicalInventoryCountItem.objects.bulk_create(
        [
            PhysicalInventoryCountItem(
                tenant=tenant, count=count, product=product,
                expected_quantity=product.current_stock,
            )
            for product in products
        ]
    )
    return count


def set_counted_quantity(item, quantity):
    if item.count.status != InventoryCountStatus.IN_PROGRESS:
        raise ValidationError("Esta contagem já foi fechada — não é mais editável.")
    if quantity is not None and Decimal(quantity) < 0:
        raise ValidationError({"counted_quantity": "A quantidade contada não pode ser negativa."})
    item.counted_quantity = quantity
    item.save(update_fields=["counted_quantity"])
    return item


@transaction.atomic
def close_inventory_count(count, *, created_by):
    """RF46 — fecha a contagem: todo item com `counted_quantity` diferente
    de `expected_quantity` vira um `StockMovement` automático
    (`reason=adjustment`) — reaproveita `register_stock_movement`, sem
    lógica de estoque duplicada. Itens deixados em branco são ignorados
    (produto não foi contado nesta rodada)."""
    if count.status != InventoryCountStatus.IN_PROGRESS:
        raise ValidationError("Esta contagem já foi fechada.")

    for item in count.items.select_related("product").filter(counted_quantity__isnull=False):
        difference = item.counted_quantity - item.expected_quantity
        if difference == 0:
            continue
        register_stock_movement(
            tenant=count.tenant,
            product=item.product,
            movement_type=MovementType.IN if difference > 0 else MovementType.OUT,
            quantity=abs(difference),
            unit_price=item.product.cost_price,
            reason=MovementReason.ADJUSTMENT,
            created_by=created_by,
        )

    count.status = InventoryCountStatus.COMPLETED
    count.completed_at = timezone.now()
    count.save(update_fields=["status", "completed_at"])
    return count
