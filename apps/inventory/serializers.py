from decimal import Decimal

from rest_framework import serializers

from .models import (
    Category,
    MovementReason,
    MovementType,
    Product,
    ProductBatch,
    StockMovement,
    Supplier,
)


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ["id", "name", "contact_name", "phone", "email", "notes", "is_active"]
        read_only_fields = ["id", "is_active"]


class ProductSerializer(serializers.ModelSerializer):
    is_low_stock = serializers.BooleanField(read_only=True)
    category = serializers.PrimaryKeyRelatedField(queryset=Category.objects.all())
    supplier = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "sku",
            "category",
            "supplier",
            "unit",
            "cost_price",
            "sale_price",
            "current_stock",
            "min_stock_alert",
            "tracks_batches",
            "is_for_sale",
            "is_active",
            "is_low_stock",
        ]
        read_only_fields = ["id", "current_stock", "is_active", "is_low_stock"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("O nome do produto é obrigatório.")
        return value


class StockMovementSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "product",
            "product_name",
            "type",
            "quantity",
            "unit_price",
            "total_value",
            "reason",
            "supplier",
            "created_at",
        ]
        read_only_fields = ["id", "total_value", "created_at"]


class ProductBatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductBatch
        fields = [
            "id",
            "product",
            "batch_number",
            "expiry_date",
            "quantity_received",
            "quantity_remaining",
            "supplier",
            "unit_cost",
            "received_at",
        ]
        read_only_fields = ["id", "quantity_remaining", "received_at"]


class StockMovementCreateSerializer(serializers.Serializer):
    """Entrada manual (RF17): compra, ajuste ou perda — vendas/uso em
    serviço vêm automaticamente da conclusão de atendimento (Etapa 7)."""

    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    type = serializers.ChoiceField(choices=MovementType.choices)
    quantity = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01")
    )
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    supplier = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.all(), required=False, allow_null=True
    )
    reason = serializers.ChoiceField(
        choices=[
            (MovementReason.PURCHASE, MovementReason.PURCHASE.label),
            (MovementReason.ADJUSTMENT, MovementReason.ADJUSTMENT.label),
            (MovementReason.LOSS, MovementReason.LOSS.label),
        ]
    )
    batch_number = serializers.CharField(max_length=60, required=False, allow_blank=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
