from decimal import Decimal

from django import forms

from apps.finance.forms import REAL_MONEY_METHODS
from apps.tenants.forms import BRDecimalField


class ClientForm(forms.Form):
    """Cadastro/edição de cliente pelo painel — a regra de negócio vive em
    apps/clients/services.py (create_client/update_client)."""

    name = forms.CharField(max_length=120, label="Nome")
    phone = forms.CharField(max_length=20, label="Telefone")
    preferences = forms.CharField(
        required=False, widget=forms.Textarea, label="Preferências"
    )


class SubscriptionForm(forms.Form):
    """Habilita/desabilita mensalista — vencimento obrigatório ao habilitar
    (validado em apps/clients/services.py::set_subscriber_status também, esta
    checagem no form é só pra dar um erro amigável mais cedo)."""

    is_subscriber = forms.BooleanField(required=False, label="Mensalista")
    subscription_due_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Vencimento da mensalidade",
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_subscriber") and not cleaned.get("subscription_due_date"):
            self.add_error(
                "subscription_due_date", "Informe a data de vencimento da mensalidade."
            )
        return cleaned


class AddCreditForm(forms.Form):
    amount = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), label="Valor"
    )
    payment_method = forms.ChoiceField(choices=REAL_MONEY_METHODS, label="Forma de pagamento")


class RemoveCreditForm(forms.Form):
    amount = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), label="Valor"
    )
    reason = forms.CharField(
        max_length=255, required=False, label="Motivo",
        widget=forms.TextInput(attrs={"placeholder": "Ex.: estorno em dinheiro, correção"}),
    )
