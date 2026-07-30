from rest_framework import serializers

from .models import Tenant, TenantBusinessHours


class TenantBusinessHoursSerializer(serializers.ModelSerializer):
    weekday_display = serializers.CharField(source="get_weekday_display", read_only=True)

    class Meta:
        model = TenantBusinessHours
        fields = ["weekday", "weekday_display", "is_closed", "start_time", "end_time"]


class TenantSettingsSerializer(serializers.ModelSerializer):
    # Somente leitura por enquanto — quem escreve horário de funcionamento é
    # o painel (formset em /painel/configuracoes/), a API só espelha (regra
    # do CLAUDE.md de a API refletir o painel, mas edição via API mobile
    # ainda não foi pedida).
    business_hours = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            "id",
            "name",
            "slug",
            "whatsapp",
            "address",
            "description",
            "business_hours",
            "logo",
            "cover_image",
            "background_image",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "is_active", "created_at"]

    def get_business_hours(self, obj):
        hours = TenantBusinessHours.objects.for_tenant(obj).order_by("weekday")
        return TenantBusinessHoursSerializer(hours, many=True).data

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("O nome do salão é obrigatório.")
        return value
