from decimal import Decimal

from django import forms

from apps.tenants.forms import BRDecimalField

from .models import ExpenseCategory, PaymentMethod

# Formas de pagamento com dinheiro real — exclui "Crédito do cliente", que só
# faz sentido na comanda (despesa/comissão nunca são pagas com saldo de cliente).
REAL_MONEY_METHODS = [
    (value, label)
    for value, label in PaymentMethod.choices
    if value != PaymentMethod.CLIENT_CREDIT
]


class ExpenseForm(forms.Form):
    """RF23: despesa avulsa (aluguel, contas, etc.)."""

    amount = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), label="Valor"
    )
    payment_method = forms.ChoiceField(choices=REAL_MONEY_METHODS, label="Forma de pagamento")
    description = forms.CharField(max_length=255, label="Descrição")
    expense_category = forms.ModelChoiceField(
        queryset=ExpenseCategory.objects.none(), required=False, label="Categoria",
        help_text="Opcional — sem categoria, a despesa não entra na quebra fixo/variável do DRE.",
    )

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tenant is not None:
            self.fields["expense_category"].queryset = ExpenseCategory.objects.for_tenant(
                tenant
            ).filter(is_active=True)


class ExpenseCategoryForm(forms.ModelForm):
    """Validação de entrada do painel — a regra de negócio vive em services.py.

    `is_fixed` é `TypedChoiceField` (não o `BooleanField` que o ModelForm
    geraria sozinho) de propósito — `BooleanField` nasce `required=True` por
    padrão, o que rejeitaria a escolha "Variável" (`False`) como se o campo
    estivesse vazio."""

    is_fixed = forms.TypedChoiceField(
        choices=[("True", "Fixa"), ("False", "Variável")],
        coerce=lambda value: value == "True",
        label="Tipo de despesa",
    )

    class Meta:
        model = ExpenseCategory
        fields = ["name", "is_fixed"]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("O nome da categoria é obrigatório.")
        return name


class PayCommissionForm(forms.Form):
    payment_method = forms.ChoiceField(choices=REAL_MONEY_METHODS, label="Forma de pagamento")
