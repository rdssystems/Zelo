import datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.accounts.decorators import tenant_admin_required
from apps.accounts.permissions import IsTenantAdminOrReadOnly, IsTenantMember
from apps.finance.models import CommissionStatus
from apps.services.models import Service

from . import services as employee_ops
from .forms import EmployeeForm
from .models import Employee, Weekday
from .serializers import EmployeeCreateSerializer, EmployeeSerializer

# ---------------------------------------------------------------------------
# API REST (DRF) — /api/v1/employees/
# ---------------------------------------------------------------------------


class EmployeeViewSet(viewsets.ModelViewSet):
    # request.user.tenant (e não request.tenant): autenticação DRF roda depois
    # do middleware de tenant — ver comentário em apps/services/views.py
    serializer_class = EmployeeSerializer
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return (
            Employee.objects.for_tenant(self.request.user.tenant)
            .select_related("user")
            .prefetch_related("working_hours", "service_links__service")
        )

    def create(self, request, *args, **kwargs):
        serializer = EmployeeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            employee = employee_ops.create_employee(
                tenant=request.user.tenant, **serializer.validated_data
            )
        except ValidationError as exc:
            return Response(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            EmployeeSerializer(employee).data, status=status.HTTP_201_CREATED
        )

    def partial_update(self, request, *args, **kwargs):
        employee = self.get_object()
        data = request.data
        try:
            employee_ops.update_employee(
                employee,
                full_name=data.get("full_name", employee.full_name),
                phone=data.get("phone", employee.phone),
                bio=data.get("bio", employee.bio),
                default_commission_type=data.get(
                    "default_commission_type", employee.default_commission_type
                ),
                default_commission_value=data.get(
                    "default_commission_value", employee.default_commission_value
                ),
                password=data.get("password") or None,
            )
        except ValidationError as exc:
            return Response(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(EmployeeSerializer(employee).data)

    @action(detail=True, methods=["post"])
    def toggle(self, request, pk=None):
        employee = self.get_object()
        employee_ops.set_employee_active(employee, not employee.is_active)
        return Response(EmployeeSerializer(employee).data)

    def perform_destroy(self, instance):
        try:
            employee_ops.delete_employee(instance)
        except ValidationError as exc:
            raise DRFValidationError(exc.messages)


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/funcionarios/
# ---------------------------------------------------------------------------


def _get_employee(request, pk):
    return get_object_or_404(
        Employee.objects.for_tenant(request.tenant).select_related("user"),
        pk=pk,
    )


@tenant_admin_required
def employee_list(request):
    employees = (
        Employee.objects.for_tenant(request.tenant)
        .select_related("user")
        .prefetch_related("service_links__service")
    )
    return render(
        request,
        "painel/employees/list.html",
        {"employees": employees, "active_nav": "employees"},
    )


@tenant_admin_required
def employee_create(request):
    # Checa a vaga ANTES de mostrar o formulário — sem isso o admin só
    # descobria que o plano estava cheio depois de preencher tudo e
    # submeter (a checagem de verdade continua em create_employee, esta
    # aqui é só pra avisar cedo).
    from apps.billing.services import assert_can_add_employee

    try:
        assert_can_add_employee(request.tenant)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("employees:list")

    form = EmployeeForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data.copy()
        password = data.pop("password1")
        data.pop("password2")
        try:
            employee = employee_ops.create_employee(
                tenant=request.tenant, password=password, **data
            )
        except ValidationError as exc:
            for field, errors in getattr(exc, "message_dict", {"__all__": exc.messages}).items():
                field = "password1" if field == "password" else field
                for error in errors:
                    form.add_error(field if field in form.fields else None, error)
        else:
            messages.success(
                request,
                f"Funcionário criado. Login: {employee.user.email}.",
            )
            return redirect("employees:update", pk=employee.pk)
    return render(
        request,
        "painel/employees/form.html",
        {"form": form, "active_nav": "employees", "employee": None},
    )


@tenant_admin_required
def employee_update(request, pk):
    employee = _get_employee(request, pk)
    initial = {
        "full_name": employee.full_name,
        "email": employee.user.email,
        "phone": employee.phone,
        "bio": employee.bio,
        "default_commission_type": employee.default_commission_type,
        "default_commission_value": employee.default_commission_value,
    }
    form = EmployeeForm(
        request.POST or None, request.FILES or None, initial=initial, editing=True
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        employee_ops.update_employee(
            employee,
            full_name=data["full_name"],
            phone=data["phone"],
            bio=data["bio"],
            photo=data["photo"],
            default_commission_type=data["default_commission_type"],
            default_commission_value=data["default_commission_value"],
            password=data["password1"] or None,
        )
        messages.success(request, "Dados do funcionário atualizados.")
        return redirect("employees:update", pk=employee.pk)
    return render(
        request,
        "painel/employees/form.html",
        {
            "form": form,
            "employee": employee,
            "active_nav": "employees",
            "weekdays": _hours_context(employee),
            "any_day_has_break": _any_day_has_break(employee),
            "services_ctx": _services_context(employee),
            "paid_commissions": _commissions_context(employee),
        },
    )


def _items_response(request):
    """Lista atualizada + limpeza do modal (swap out-of-band do HTMX)."""
    employees = (
        Employee.objects.for_tenant(request.tenant)
        .select_related("user")
        .prefetch_related("service_links__service")
    )
    items = render_to_string(
        "painel/employees/_items.html", {"employees": employees}, request=request
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset)


@tenant_admin_required
def employee_toggle_confirm(request, pk):
    employee = _get_employee(request, pk)
    if not employee.is_active:
        from apps.billing.services import assert_can_add_employee

        try:
            assert_can_add_employee(request.tenant)
        except ValidationError as exc:
            return render(
                request,
                "painel/employees/_seat_limit_modal.html",
                {"message": " ".join(exc.messages)},
            )
    if employee.is_active:
        context = {
            "title": "Desativar funcionário",
            "message": (
                f"Desativar {employee.full_name}? O login será bloqueado e o "
                f"profissional some da página pública de agendamento."
            ),
            "icon": "toggle_off",
            "confirm_label": "Desativar",
        }
    else:
        context = {
            "title": "Reativar funcionário",
            "message": f"Reativar {employee.full_name}? O login volta a funcionar.",
            "icon": "toggle_on",
            "confirm_label": "Reativar",
        }
    context.update(
        {
            "action_url": reverse("employees:toggle", args=[employee.pk]),
            "target": "#employee-items",
        }
    )
    return render(request, "painel/_confirm_action.html", context)


@tenant_admin_required
def employee_toggle(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    employee = _get_employee(request, pk)
    try:
        employee_ops.set_employee_active(employee, not employee.is_active)
    except ValidationError as exc:
        # Vaga lotada entre abrir o modal e clicar em "Reativar" (ex.: duas
        # abas) — employee_toggle_confirm já pré-checa, isso é só defesa.
        response = render(
            request,
            "painel/employees/_seat_limit_modal.html",
            {"message": " ".join(exc.messages)},
        )
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
        return response
    return _items_response(request)


@tenant_admin_required
def employee_delete_confirm(request, pk):
    employee = _get_employee(request, pk)
    return render(
        request,
        "painel/_confirm_delete.html",
        {
            "title": "Excluir funcionário",
            "item_label": employee.full_name,
            "warning": "O acesso ao painel, a jornada e os vínculos de serviço deste profissional serão removidos junto.",
            "delete_url": reverse("employees:delete", args=[employee.pk]),
            "target": "#employee-items",
        },
    )


@tenant_admin_required
def employee_delete(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    employee = _get_employee(request, pk)
    try:
        employee_ops.delete_employee(employee)
    except ValidationError:
        return HttpResponse(status=409)
    return _items_response(request)


def _hours_context(employee):
    """Uma linha por dia da semana com a jornada atual — 2 turnos por dia,
    o 2º opcional pra intervalo recorrente (decisão do usuário em
    2026-08-06, ex. pausa de almoço todo dia sem precisar cadastrar
    ScheduleException manualmente)."""
    current = {wh.weekday: wh for wh in employee.working_hours.all()}
    return [
        {
            "weekday": value,
            "label": label,
            "entry": current.get(value),
        }
        for value, label in Weekday.choices
    ]


def _any_day_has_break(employee):
    """Controla se o botão "Marcar intervalo" já nasce ligado — evita
    esconder um intervalo já cadastrado atrás de um clique extra."""
    return employee.working_hours.filter(start_time_2__isnull=False).exists()


def _services_context(employee):
    links = {link.service_id: link for link in employee.service_links.all()}
    return [
        {"service": service, "link": links.get(service.pk)}
        for service in Service.objects.for_tenant(employee.tenant).filter(
            is_active=True
        )
    ]


def _commissions_context(employee):
    """Histórico de comissões já pagas deste funcionário (mais recentes
    primeiro) — visão do admin no perfil, complementa a aba Comissões do
    Caixa (apps/finance) sem duplicar a lógica de pagamento."""
    return (
        employee.commissions.filter(status=CommissionStatus.PAID)
        .select_related("appointment__service")
        .order_by("-paid_at")
    )


@tenant_admin_required
def employee_working_hours(request, pk):
    """POST HTMX: substitui a jornada semanal (checkbox + início/fim por dia)."""
    if request.method != "POST":
        return HttpResponse(status=405)
    employee = _get_employee(request, pk)
    entries, error = [], None
    for value, label in Weekday.choices:
        if not request.POST.get(f"day_{value}_active"):
            continue
        try:
            start = datetime.time.fromisoformat(request.POST[f"day_{value}_start"])
            end = datetime.time.fromisoformat(request.POST[f"day_{value}_end"])
        except (KeyError, ValueError):
            error = f"Preencha início e fim válidos para {label}."
            break
        # Intervalo (2º turno) é opcional por dia mesmo com o botão "Marcar
        # intervalo" ligado — campo em branco vira None, não erro.
        start_2_raw = request.POST.get(f"day_{value}_start_2", "").strip()
        end_2_raw = request.POST.get(f"day_{value}_end_2", "").strip()
        try:
            start_2 = datetime.time.fromisoformat(start_2_raw) if start_2_raw else None
            end_2 = datetime.time.fromisoformat(end_2_raw) if end_2_raw else None
        except ValueError:
            error = f"Horário de intervalo inválido para {label}."
            break
        entries.append({
            "weekday": value, "start_time": start, "end_time": end,
            "start_time_2": start_2, "end_time_2": end_2,
        })
    if error is None:
        try:
            employee_ops.set_working_hours(employee, entries)
        except ValidationError as exc:
            error = " ".join(exc.messages)
    employee.refresh_from_db()
    return render(
        request,
        "painel/employees/_hours.html",
        {
            "employee": employee,
            "weekdays": _hours_context(employee),
            "any_day_has_break": _any_day_has_break(employee),
            "hours_error": error,
            "hours_saved": error is None,
        },
    )


@tenant_admin_required
def employee_services(request, pk):
    """POST HTMX: define os vínculos com serviços (+ override de comissão)."""
    if request.method != "POST":
        return HttpResponse(status=405)
    employee = _get_employee(request, pk)
    error = None
    try:
        for service in Service.objects.for_tenant(request.tenant).filter(
            is_active=True
        ):
            if request.POST.get(f"service_{service.pk}_linked"):
                employee_ops.link_service(
                    employee,
                    service,
                    commission_type=request.POST.get(
                        f"service_{service.pk}_ctype"
                    )
                    or None,
                    commission_value=request.POST.get(
                        f"service_{service.pk}_cvalue"
                    )
                    or None,
                )
            else:
                employee_ops.unlink_service(employee, service)
    except ValidationError as exc:
        error = " ".join(exc.messages)
    return render(
        request,
        "painel/employees/_services.html",
        {
            "employee": employee,
            "services_ctx": _services_context(employee),
            "services_error": error,
            "services_saved": error is None,
        },
    )
