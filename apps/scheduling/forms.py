import datetime

from django import forms

from apps.clients.forms import BIRTH_DAY_CHOICES, BIRTH_MONTH_CHOICES
from apps.employees.models import Employee
from apps.services.models import Service


class NewAppointmentForm(forms.Form):
    """RF17: encaixe manual (admin/funcionário), cliente por telefone/balcão."""

    service = forms.ModelChoiceField(queryset=Service.objects.none(), label="Serviço")
    employee = forms.ModelChoiceField(queryset=Employee.objects.none(), label="Profissional")
    date = forms.DateField(label="Data", widget=forms.DateInput(attrs={"type": "date"}))
    time = forms.TimeField(label="Horário", widget=forms.TimeInput(attrs={"type": "time"}))
    phone = forms.CharField(max_length=20, label="Telefone do cliente")
    name = forms.CharField(
        max_length=120, required=False, label="Nome do cliente (se for novo)"
    )
    # `required=False` aqui: a exigência de verdade (quando
    # Tenant.require_birthday_on_booking está ligado) é validada no backend
    # por `get_or_create_client`, e só se aplica quando o telefone é de um
    # cliente NOVO — cliente já cadastrado não deve ser bloqueado por um
    # campo HTML `required` que nem chega a ser usado nesse caso.
    birth_day = forms.TypedChoiceField(
        choices=BIRTH_DAY_CHOICES, coerce=int, required=False, empty_value=None,
        label="Dia de nascimento (se for novo)",
    )
    birth_month = forms.TypedChoiceField(
        choices=BIRTH_MONTH_CHOICES, coerce=int, required=False, empty_value=None,
        label="Mês de nascimento (se for novo)",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea, label="Observações")

    def __init__(self, *args, tenant=None, lock_employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = Service.objects.for_tenant(tenant).filter(
            is_active=True
        )
        if lock_employee is not None:
            # Funcionário agendando por conta própria só pode escolher a si
            # mesmo (decisão do usuário em 2026-08-07) — restringir o
            # queryset aqui também barra quem tentar forjar outro `pk` no
            # POST (`ModelChoiceField` rejeita valor fora do queryset).
            self.fields["employee"].queryset = Employee.objects.filter(
                pk=lock_employee.pk
            )
            self.initial["employee"] = lock_employee.pk
        else:
            self.fields["employee"].queryset = Employee.objects.for_tenant(tenant).filter(
                is_active=True
            )

    def clean_date(self):
        date = self.cleaned_data["date"]
        if date < datetime.date.today():
            raise forms.ValidationError("Não é possível agendar em uma data passada.")
        return date
