from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from apps.tenants.forms import BRDecimalField

from .models import CommissionType


class EmployeeForm(forms.Form):
    """Entrada do painel — a criação (com User automático) vive em services.py."""

    full_name = forms.CharField(max_length=120, label="Nome completo")
    email = forms.EmailField(label="E-mail de acesso")
    password1 = forms.CharField(
        required=False, widget=forms.PasswordInput, label="Senha"
    )
    password2 = forms.CharField(
        required=False, widget=forms.PasswordInput, label="Confirmar senha"
    )
    phone = forms.CharField(max_length=20, required=False, label="Telefone")
    bio = forms.CharField(required=False, widget=forms.Textarea, label="Bio")
    photo = forms.ImageField(required=False, label="Foto")
    default_commission_type = forms.ChoiceField(
        choices=CommissionType.choices, label="Tipo de comissão padrão"
    )
    default_commission_value = BRDecimalField(
        max_digits=10, decimal_places=2, min_value=0, label="Valor da comissão padrão"
    )

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._editing = editing
        if editing:
            # e-mail de login não é editável pelo painel no MVP
            self.fields["email"].required = False
            self.fields["email"].disabled = True
            self.fields["password1"].help_text = "Deixe em branco para manter a senha atual."

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if not self._editing and not password1:
            self.add_error("password1", "A senha é obrigatória.")
        elif password1 != password2:
            self.add_error("password2", "As senhas não coincidem.")
        elif password1:
            try:
                validate_password(password1)
            except DjangoValidationError as exc:
                for message in exc.messages:
                    self.add_error("password1", message)
        return cleaned
