import datetime

from django import forms

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
    notes = forms.CharField(required=False, widget=forms.Textarea, label="Observações")

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = Service.objects.for_tenant(tenant).filter(
            is_active=True
        )
        self.fields["employee"].queryset = Employee.objects.for_tenant(tenant).filter(
            is_active=True
        )

    def clean_date(self):
        date = self.cleaned_data["date"]
        if date < datetime.date.today():
            raise forms.ValidationError("Não é possível agendar em uma data passada.")
        return date
