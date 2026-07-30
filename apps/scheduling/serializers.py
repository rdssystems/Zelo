from rest_framework import serializers

from apps.employees.models import Employee
from apps.finance.models import PaymentMethod
from apps.services.models import Service

from .models import Appointment


class AppointmentSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True)

    class Meta:
        model = Appointment
        fields = [
            "id",
            "client",
            "client_name",
            "employee",
            "employee_name",
            "service",
            "service_name",
            "date",
            "start_time",
            "end_time",
            "status",
            "price_at_booking",
            "notes",
        ]
        read_only_fields = ["id", "end_time", "status", "price_at_booking"]


class AppointmentCreateSerializer(serializers.Serializer):
    """RF17: encaixe manual — cliente identificado por telefone (RF04)."""

    service = serializers.PrimaryKeyRelatedField(queryset=Service.objects.all())
    employee = serializers.PrimaryKeyRelatedField(queryset=Employee.objects.all())
    date = serializers.DateField()
    time = serializers.TimeField()
    phone = serializers.CharField(max_length=20)
    name = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class ProductUsageItemSerializer(serializers.Serializer):
    """Preço unitário não entra aqui — vem sempre de `Product.sale_price`
    (ver `apps.scheduling.services.build_product_usage`)."""

    product = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2)


class CompleteAppointmentSerializer(serializers.Serializer):
    payment_method = serializers.ChoiceField(choices=PaymentMethod.choices)
    items = ProductUsageItemSerializer(many=True, required=False, default=list)
