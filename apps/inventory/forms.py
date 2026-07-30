from decimal import Decimal

from django import forms

from apps.tenants.forms import BRDecimalField

from .models import Category, MovementReason, MovementType, ProductUnit, Supplier


class ProductForm(forms.Form):
    """Entrada do painel — a regra de negócio vive em services.py."""

    name = forms.CharField(max_length=120, label="Nome do produto")
    sku = forms.CharField(max_length=40, required=False, label="SKU")
    category = forms.ModelChoiceField(queryset=Category.objects.none(), label="Categoria")
    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.none(), label="Fornecedor preferido", required=False
    )
    unit = forms.ChoiceField(choices=ProductUnit.choices, label="Unidade")
    cost_price = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=0, label="Preço de custo"
    )
    sale_price = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=0, label="Preço de venda"
    )
    min_stock_alert = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=0, label="Estoque mínimo"
    )
    tracks_batches = forms.BooleanField(
        required=False, label="Controla lote/validade (produto vence)"
    )

    def __init__(self, *args, tenant=None, cost_price_locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.for_tenant(tenant)
        self.fields["supplier"].queryset = Supplier.objects.for_tenant(tenant).filter(
            is_active=True
        )
        if cost_price_locked:
            # RF45 — depois da 1ª compra, custo médio só muda via
            # recálculo automático (não é só cosmético: `disabled=True`
            # faz o Django ignorar o valor submetido e usar `initial`).
            self.fields["cost_price"].disabled = True

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("O nome do produto é obrigatório.")
        return name


class CategoryForm(forms.Form):
    name = forms.CharField(max_length=80, label="Nome da categoria")

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("O nome da categoria é obrigatório.")
        return name


class SupplierForm(forms.Form):
    name = forms.CharField(max_length=120, label="Nome do fornecedor")
    contact_name = forms.CharField(max_length=120, required=False, label="Contato")
    phone = forms.CharField(max_length=20, required=False, label="Telefone")
    email = forms.EmailField(required=False, label="E-mail")
    notes = forms.CharField(required=False, label="Observações", widget=forms.Textarea)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("O nome do fornecedor é obrigatório.")
        return name


class StockMovementForm(forms.Form):
    """Movimentação manual (RF17): compra, ajuste ou perda."""

    MANUAL_REASON_CHOICES = [
        (MovementReason.PURCHASE, MovementReason.PURCHASE.label),
        (MovementReason.ADJUSTMENT, MovementReason.ADJUSTMENT.label),
        (MovementReason.LOSS, MovementReason.LOSS.label),
    ]

    type = forms.ChoiceField(choices=MovementType.choices, label="Tipo")
    reason = forms.ChoiceField(choices=MANUAL_REASON_CHOICES, label="Motivo")
    quantity = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), label="Quantidade"
    )
    unit_price = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=0, label="Preço unitário"
    )
    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.none(), label="Fornecedor desta compra", required=False
    )
    batch_number = forms.CharField(max_length=60, required=False, label="Número do lote")
    expiry_date = forms.DateField(required=False, label="Validade")

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.for_tenant(tenant).filter(
            is_active=True
        )
