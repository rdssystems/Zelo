from rest_framework import serializers

from .models import Service


class ServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = ["id", "name", "description", "duration_minutes", "price", "is_active"]
        read_only_fields = ["id"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("O nome do serviço é obrigatório.")
        return value

    def validate_duration_minutes(self, value):
        if value < 1:
            raise serializers.ValidationError(
                "A duração deve ser de pelo menos 1 minuto."
            )
        return value

    def validate_price(self, value):
        if value < 0:
            raise serializers.ValidationError("O preço não pode ser negativo.")
        return value
