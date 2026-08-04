from decimal import Decimal

from django import forms

from apps.finance.forms import REAL_MONEY_METHODS
from apps.services.models import Service
from apps.tenants.forms import BRDecimalField

BIRTH_MONTH_CHOICES = [
    ("", "Mês"), (1, "Jan"), (2, "Fev"), (3, "Mar"), (4, "Abr"), (5, "Mai"), (6, "Jun"),
    (7, "Jul"), (8, "Ago"), (9, "Set"), (10, "Out"), (11, "Nov"), (12, "Dez"),
]
BIRTH_DAY_CHOICES = [("", "Dia")] + [(d, d) for d in range(1, 32)]


class ClientForm(forms.Form):
    """Cadastro/edição de cliente pelo painel — a regra de negócio vive em
    apps/clients/services.py (create_client/update_client)."""

    name = forms.CharField(max_length=120, label="Nome")
    phone = forms.CharField(max_length=20, label="Telefone")
    preferences = forms.CharField(
        required=False, widget=forms.Textarea, label="Preferências"
    )
    birth_day = forms.TypedChoiceField(
        choices=BIRTH_DAY_CHOICES, coerce=int, required=False, empty_value=None,
        label="Dia de nascimento",
    )
    birth_month = forms.TypedChoiceField(
        choices=BIRTH_MONTH_CHOICES, coerce=int, required=False, empty_value=None,
        label="Mês de nascimento",
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


class PackageForm(forms.Form):
    """Cadastro/edição de pacote de mensalidade — a regra de negócio vive em
    apps/clients/services.py (create_package/update_package)."""

    name = forms.CharField(max_length=120, label="Nome do pacote")
    description = forms.CharField(max_length=255, required=False, label="Descrição")
    price = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), label="Valor mensal"
    )
    services = forms.ModelMultipleChoiceField(
        queryset=Service.objects.none(), label="Serviços inclusos no pacote",
        widget=forms.CheckboxSelectMultiple,
    )
    generates_commission = forms.BooleanField(
        required=False, initial=True, label="Gera comissão pro funcionário",
    )

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tenant is not None:
            self.fields["services"].queryset = Service.objects.for_tenant(tenant).filter(
                is_active=True
            )


class AssignPackageForm(forms.Form):
    """Ativar um pacote pro cliente — pergunta a forma de pagamento porque é
    uma cobrança real na hora (ver apps/clients/services.py::assign_package_to_client)."""

    package = forms.ModelChoiceField(queryset=None, label="Pacote", empty_label="Selecione um pacote")
    payment_method = forms.ChoiceField(choices=REAL_MONEY_METHODS, label="Forma de pagamento")

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tenant is not None:
            from .models import Package

            self.fields["package"].queryset = Package.objects.for_tenant(tenant).filter(
                is_active=True
            )


class RenewPackageForm(forms.Form):
    """Renovar mensalidade de cliente com pacote ativo — mesma cobrança real
    de `AssignPackageForm`, mas sem escolher pacote (já é o vigente, ver
    apps/clients/services.py::renew_subscription)."""

    payment_method = forms.ChoiceField(choices=REAL_MONEY_METHODS, label="Forma de pagamento")
