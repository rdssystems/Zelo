from decimal import Decimal

from rest_framework import serializers

from .models import Client, ClientCreditTransaction


class ClientSerializer(serializers.ModelSerializer):
    subscription_is_overdue = serializers.BooleanField(read_only=True)
    subscription_is_due_soon = serializers.BooleanField(read_only=True)

    class Meta:
        model = Client
        fields = [
            "id",
            "name",
            "phone",
            "preferences",
            "is_subscriber",
            "subscription_due_date",
            "subscription_is_overdue",
            "subscription_is_due_soon",
            "credit_balance",
            "created_at",
        ]
        read_only_fields = ["id", "credit_balance", "created_at"]


class ClientCreditTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientCreditTransaction
        fields = ["id", "type", "amount", "reason", "related_appointment", "created_at"]
        read_only_fields = fields


class AddCreditSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    payment_method = serializers.CharField(max_length=20)


class RemoveCreditSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class SetSubscriptionSerializer(serializers.Serializer):
    is_subscriber = serializers.BooleanField()
    subscription_due_date = serializers.DateField(required=False, allow_null=True)
