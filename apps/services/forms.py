from django import forms

from apps.tenants.forms import BRDecimalField

from .models import Service


class ServiceForm(forms.ModelForm):
    """Validação de entrada do painel — a regra de negócio vive em services.py."""

    class Meta:
        model = Service
        fields = ["name", "description", "duration_minutes", "price"]
        field_classes = {"price": BRDecimalField}

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("O nome do serviço é obrigatório.")
        return name
