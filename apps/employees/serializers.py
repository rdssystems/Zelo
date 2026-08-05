from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import CommissionType, Employee, EmployeeService, WorkingHours


class WorkingHoursSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkingHours
        fields = [
            "weekday", "start_time", "end_time",
            "start_time_2", "end_time_2", "is_active",
        ]


class EmployeeServiceLinkSerializer(serializers.ModelSerializer):
    service = serializers.PrimaryKeyRelatedField(read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True)

    class Meta:
        model = EmployeeService
        fields = ["service", "service_name", "commission_type", "commission_value"]


class EmployeeSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    working_hours = WorkingHoursSerializer(many=True, read_only=True)
    service_links = EmployeeServiceLinkSerializer(many=True, read_only=True)

    class Meta:
        model = Employee
        fields = [
            "id",
            "full_name",
            "email",
            "phone",
            "bio",
            "photo",
            "default_commission_type",
            "default_commission_value",
            "is_active",
            "working_hours",
            "service_links",
        ]
        read_only_fields = ["id", "is_active"]


class EmployeeCreateSerializer(serializers.Serializer):
    """Entrada de criação — o User de login é criado pelo domínio (RF08)."""

    full_name = serializers.CharField(max_length=120)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    bio = serializers.CharField(required=False, allow_blank=True, default="")
    default_commission_type = serializers.ChoiceField(choices=CommissionType.choices)
    default_commission_value = serializers.DecimalField(max_digits=10, decimal_places=2)
