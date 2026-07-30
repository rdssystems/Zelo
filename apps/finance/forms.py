from decimal import Decimal

from django import forms

from apps.tenants.forms import BRDecimalField

from .models import PaymentMethod

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


class PayCommissionForm(forms.Form):
    payment_method = forms.ChoiceField(choices=REAL_MONEY_METHODS, label="Forma de pagamento")
