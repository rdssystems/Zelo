from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth import password_validation

from .models import Tenant

User = get_user_model()


class SignUpForm(forms.Form):
    """Cadastro self-service do salão — cria o Tenant e o admin (RF? — ver
    apps/tenants/services.py::register_tenant)."""

    name = forms.CharField(max_length=120, label="Nome do estabelecimento")
    email = forms.EmailField(label="E-mail")
    password1 = forms.CharField(widget=forms.PasswordInput, label="Senha")
    password2 = forms.CharField(widget=forms.PasswordInput, label="Confirmar senha")
    accept_terms = forms.BooleanField(
        label="Li e aceito os Termos de Uso e a Política de Privacidade",
        required=True,
        error_messages={"required": "É preciso aceitar os Termos de Uso e a Política de Privacidade."},
    )

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("O nome do estabelecimento é obrigatório.")
        return name

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Já existe uma conta com este e-mail.")
        return email

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "As senhas não coincidem.")
        elif password1:
            try:
                password_validation.validate_password(password1)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned


class TenantSettingsForm(forms.ModelForm):
    """RF25-RF27: identidade visual, contato e slug público do salão."""

    class Meta:
        model = Tenant
        fields = [
            "name",
            "slug",
            "whatsapp",
            "address",
            "description",
            "logo",
            "cover_image",
            "background_image",
        ]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("O nome do salão é obrigatório.")
        return name


_TIME_INPUT_ATTRS = {
    "type": "time",
    "class": (
        "px-3 py-2 rounded-lg border border-outline-variant bg-surface-container-lowest "
        "text-on-surface text-sm focus:ring-2 focus:ring-primary focus:border-primary "
        "transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
    ),
}


class BusinessHoursDayForm(forms.Form):
    """1 linha do formset de horário de funcionamento — 1 por dia da semana
    (RF26). "Fechado" limpa os horários (não pode ter os dois preenchidos)."""

    weekday = forms.IntegerField(widget=forms.HiddenInput)
    is_closed = forms.BooleanField(
        required=False,
        label="Fechado",
        widget=forms.CheckboxInput(
            attrs={
                "class": "rounded border-outline text-primary focus:ring-primary",
                "x-model": "closed",
            }
        ),
    )
    start_time = forms.TimeField(
        required=False, label="Abre",
        widget=forms.TimeInput(format="%H:%M", attrs={**_TIME_INPUT_ATTRS, ":disabled": "closed"}),
    )
    end_time = forms.TimeField(
        required=False, label="Fecha",
        widget=forms.TimeInput(format="%H:%M", attrs={**_TIME_INPUT_ATTRS, ":disabled": "closed"}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_closed"):
            cleaned["start_time"] = None
            cleaned["end_time"] = None
            return cleaned
        start_time, end_time = cleaned.get("start_time"), cleaned.get("end_time")
        if not start_time or not end_time:
            raise forms.ValidationError("Informe abertura e fechamento, ou marque como fechado.")
        if start_time >= end_time:
            raise forms.ValidationError("O fechamento deve ser depois da abertura.")
        return cleaned


BusinessHoursFormSet = forms.formset_factory(BusinessHoursDayForm, extra=0)


class DeleteAccountForm(forms.Form):
    """Exclusão de conta — exige digitar a frase exata como confirmação
    (pedido explícito do usuário: barreira extra além de um clique)."""

    CONFIRMATION_PHRASE = "EXCLUIR MINHACONTA"

    confirmation = forms.CharField(
        label=f"Digite {CONFIRMATION_PHRASE} para confirmar"
    )

    def clean_confirmation(self):
        value = self.cleaned_data["confirmation"].strip()
        if value != self.CONFIRMATION_PHRASE:
            raise forms.ValidationError(
                f"Digite exatamente \"{self.CONFIRMATION_PHRASE}\" para confirmar."
            )
        return value


class BRDecimalField(forms.DecimalField):
    """DecimalField que aceita vírgula OU ponto como separador decimal.

    Campo `<input type="number">` só aceita ponto (é assim que a spec HTML5
    define, independente do idioma da página) — mas o usuário brasileiro
    digita vírgula por hábito. Os templates usam `type="text" inputmode="decimal"`
    para esses campos, e este field normaliza o valor antes de converter
    para `Decimal`.
    """

    def to_python(self, value):
        if isinstance(value, str) and "," in value:
            value = value.strip().replace(".", "").replace(",", ".")
        return super().to_python(value)
