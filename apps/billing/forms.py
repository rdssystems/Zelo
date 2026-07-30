from django import forms

from apps.tenants.forms import BRDecimalField

from .models import Plan, SubscriptionStatus


class PlanForm(forms.Form):
    name = forms.CharField(max_length=60, label="Nome do plano")
    description = forms.CharField(
        max_length=255, required=False, label="Descrição", widget=forms.Textarea
    )
    price = BRDecimalField(max_digits=8, decimal_places=2, min_value=0, label="Preço mensal")
    is_active = forms.BooleanField(required=False, label="Ativo")
    order = forms.IntegerField(min_value=0, required=False, label="Ordem de exibição")

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("O nome do plano é obrigatório.")
        return name

    def clean_order(self):
        return self.cleaned_data.get("order") or 0


class SubscriptionPlanForm(forms.Form):
    plan = forms.ModelChoiceField(
        queryset=Plan.objects.filter(is_active=True), label="Plano", required=False
    )


class SubscriptionStatusForm(forms.Form):
    status = forms.ChoiceField(choices=SubscriptionStatus.choices, label="Status")


_DATE_INPUT_CLASSES = (
    "px-4 py-2.5 rounded-lg border border-outline-variant bg-surface-container-lowest "
    "text-on-surface focus:ring-2 focus:ring-primary focus:border-primary transition-colors"
)


class SubscriptionPeriodForm(forms.Form):
    current_period_start = forms.DateField(
        label="Início do período",
        widget=forms.DateInput(attrs={"type": "date", "class": _DATE_INPUT_CLASSES}),
    )
    current_period_end = forms.DateField(
        label="Fim do período",
        widget=forms.DateInput(attrs={"type": "date", "class": _DATE_INPUT_CLASSES}),
    )

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("current_period_start")
        end = cleaned.get("current_period_end")
        if start and end and end < start:
            raise forms.ValidationError("A data fim deve ser depois da data início.")
        return cleaned


class SubscriberDeleteForm(forms.Form):
    """Barreira extra pra exclusão de conta de OUTRO tenant pelo superadmin —
    exige digitar o slug exato do salão (mais forte que uma frase genérica,
    porque impede excluir a linha errada por engano numa lista longa)."""

    confirmation = forms.CharField(label="Digite o slug do salão para confirmar")

    def __init__(self, *args, tenant_slug=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant_slug = tenant_slug

    def clean_confirmation(self):
        value = self.cleaned_data["confirmation"].strip()
        if value != self.tenant_slug:
            raise forms.ValidationError(
                f'Digite exatamente "{self.tenant_slug}" para confirmar.'
            )
        return value
