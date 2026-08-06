from decimal import Decimal

from rest_framework import serializers

from .models import CashTransaction, Commission, ExpenseCategory, PaymentMethod


class CashTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CashTransaction
        fields = [
            "id", "type", "category", "amount", "payment_method", "description",
            "expense_category", "created_at",
        ]
        read_only_fields = ["id", "type", "category", "created_at"]


class ExpenseCreateSerializer(serializers.Serializer):
    """RF23 — a API só permite criar despesas avulsas via este endpoint;
    entradas de venda/comissão nascem automaticamente da conclusão de
    atendimento (`complete_appointment`)."""

    amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01")
    )
    payment_method = serializers.ChoiceField(choices=PaymentMethod.choices)
    description = serializers.CharField(max_length=255)
    # Opcional — a checagem de tenant acontece em
    # apps.finance.services.create_cash_transaction, não aqui (mesmo padrão
    # de outras FKs recebidas por PK direto na API).
    expense_category = serializers.PrimaryKeyRelatedField(
        queryset=ExpenseCategory.objects.all(), required=False, allow_null=True
    )


class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ["id", "name", "is_fixed", "is_active"]


class CommissionSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    class Meta:
        model = Commission
        fields = [
            "id",
            "employee",
            "employee_name",
            "appointment",
            "commission_type",
            "commission_value",
            "base_amount",
            "calculated_amount",
            "status",
            "paid_at",
        ]
        read_only_fields = fields


class PayCommissionSerializer(serializers.Serializer):
    payment_method = serializers.ChoiceField(choices=PaymentMethod.choices)
