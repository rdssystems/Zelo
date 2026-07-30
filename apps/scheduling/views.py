import datetime

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.accounts.decorators import employee_required, tenant_admin_required
from apps.accounts.permissions import IsTenantAdminOrReadOnly, IsTenantMember
from apps.clients.services import get_or_create_client
from apps.employees.models import Employee
from apps.services.models import Service
from apps.tenants.models import TenantBusinessHours

from .availability import get_available_slots_for_day
from .forms import NewAppointmentForm
from .models import Appointment, AppointmentStatus
from .serializers import (
    AppointmentCreateSerializer,
    AppointmentSerializer,
    CompleteAppointmentSerializer,
)
from .services import (
    build_product_usage,
    cancel_appointment,
    complete_appointment,
    confirm_appointment,
    create_appointment,
    mark_no_show,
    start_appointment,
)

# ---------------------------------------------------------------------------
# API REST (DRF) — /api/v1/appointments/
# ---------------------------------------------------------------------------


class AppointmentViewSet(viewsets.ModelViewSet):
    serializer_class = AppointmentSerializer
    permission_classes = [IsTenantMember, IsTenantAdminOrReadOnly]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return Appointment.objects.for_tenant(self.request.user.tenant).select_related(
            "client", "employee", "service"
        )

    def create(self, request, *args, **kwargs):
        serializer = AppointmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        tenant = request.user.tenant
        try:
            client, _created = get_or_create_client(
                tenant=tenant, phone=data["phone"], name=data["name"]
            )
            appointment = create_appointment(
                tenant=tenant,
                client=client,
                employee=data["employee"],
                service=data["service"],
                date=data["date"],
                start_time=data["time"],
                created_by=request.user,
                notes=data["notes"],
            )
        except ValidationError as exc:
            raise DRFValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            )
        return Response(AppointmentSerializer(appointment).data, status=201)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        appointment = self.get_object()
        try:
            confirm_appointment(appointment)
        except ValidationError as exc:
            raise DRFValidationError(exc.messages)
        return Response(AppointmentSerializer(appointment).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        appointment = self.get_object()
        try:
            cancel_appointment(appointment)
        except ValidationError as exc:
            raise DRFValidationError(exc.messages)
        return Response(AppointmentSerializer(appointment).data)

    @action(detail=True, methods=["post"], url_path="no-show")
    def no_show(self, request, pk=None):
        appointment = self.get_object()
        try:
            mark_no_show(appointment)
        except ValidationError as exc:
            raise DRFValidationError(exc.messages)
        return Response(AppointmentSerializer(appointment).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        appointment = self.get_object()
        serializer = CompleteAppointmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            product_usage = build_product_usage(
                tenant=request.user.tenant,
                product_ids=[str(item["product"]) for item in data["items"]],
                quantities=[item["quantity"] for item in data["items"]],
            )
            complete_appointment(
                appointment=appointment,
                payment_method=data["payment_method"],
                created_by=request.user,
                product_usage=product_usage,
            )
        except ValidationError as exc:
            raise DRFValidationError(exc.messages)
        return Response(AppointmentSerializer(appointment).data)


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/agenda/
# ---------------------------------------------------------------------------


def _parse_date(raw):
    try:
        return datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        return datetime.date.today()


def _get_appointment(request, pk):
    return get_object_or_404(
        Appointment.objects.for_tenant(request.tenant).select_related(
            "client", "employee", "service"
        ),
        pk=pk,
    )


def _employee_filter_id(request):
    """Filtro por funcionário compartilhado entre visão dia/semana — string
    vazia (ou ausente) significa "todos"."""
    raw = request.GET.get("employee", "")
    return raw if raw else ""


def _agenda_response(request, date):
    employee_id = _employee_filter_id(request)
    appointments = (
        Appointment.objects.for_tenant(request.tenant)
        .filter(date=date)
        .select_related("client", "employee", "service")
        .order_by("start_time")
    )
    if employee_id:
        appointments = appointments.filter(employee_id=employee_id)
    items = render_to_string(
        "painel/scheduling/_items.html",
        {"appointments": appointments, "selected_date": date, "selected_employee_id": employee_id},
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(items + modal_reset)


@tenant_admin_required
def agenda_list(request):
    date = _parse_date(request.GET.get("date"))
    employee_id = _employee_filter_id(request)
    appointments = (
        Appointment.objects.for_tenant(request.tenant)
        .filter(date=date)
        .select_related("client", "employee", "service")
        .order_by("start_time")
    )
    if employee_id:
        appointments = appointments.filter(employee_id=employee_id)
    return render(
        request,
        "painel/scheduling/list.html",
        {
            "appointments": appointments,
            "selected_date": date,
            "prev_date": date - datetime.timedelta(days=1),
            "next_date": date + datetime.timedelta(days=1),
            "employees": Employee.objects.for_tenant(request.tenant).filter(is_active=True).order_by("full_name"),
            "selected_employee_id": employee_id,
            "active_nav": "agenda",
        },
    )


# ---------------------------------------------------------------------------
# Painel (HTMX) — /painel/agenda/semana/ (visão semanal estilo Google Calendar)
# ---------------------------------------------------------------------------

WEEK_PX_PER_MINUTE = 1.6  # ~96px por hora — legível sem ficar gigante
_WEEK_DEFAULT_RANGE_START = datetime.time(8, 0)
_WEEK_DEFAULT_RANGE_END = datetime.time(20, 0)
_WEEK_MIN_EVENT_HEIGHT_PX = 28

_WEEKDAY_LABELS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

_STATUS_CHIP_CLASSES = {
    "pending": "bg-surface-container-high border-outline-variant text-on-surface-variant",
    "confirmed": "bg-secondary-container border-secondary/30 text-on-secondary-container",
    "in_progress": "bg-primary-container/60 border-primary/30 text-on-primary-container",
    "completed": "bg-[#e8f5e9] border-[#c8e6c9] text-[#2e7d32]",
    "canceled": "bg-error-container/70 border-error/20 text-on-error-container line-through",
    "no_show": "bg-error-container/70 border-error/20 text-on-error-container",
}


def _week_start_for(any_date):
    return any_date - datetime.timedelta(days=any_date.weekday())


def _minutes(t):
    return t.hour * 60 + t.minute


def _px(value):
    """Formata um valor numérico pra uso em CSS (`top: {{ }}px`) como string
    com ponto decimal — sem isso, o template localiza floats com vírgula
    (LANGUAGE_CODE=pt-br) e gera CSS inválido tipo `top: 96,0px`, que o
    navegador descarta silenciosamente (some qualquer posicionamento)."""
    return f"{value:.2f}"


def _round_hour_floor(t):
    return datetime.time(t.hour, 0)


def _round_hour_ceil(t):
    if t.minute == 0:
        return t
    if t.hour == 23:
        return datetime.time(23, 59)
    return datetime.time(t.hour + 1, 0)


def _week_time_range(tenant, appointments):
    """Amplitude de horário exibida na grade — cobre o horário de
    funcionamento configurado (Configurações) e qualquer agendamento fora
    dele (ex.: encaixe excepcional), arredondada pra hora cheia. Sem nenhum
    dado, cai no padrão 08:00–20:00."""
    open_hours = TenantBusinessHours.objects.for_tenant(tenant).filter(is_closed=False)
    starts = [h.start_time for h in open_hours] + [a.start_time for a in appointments]
    ends = [h.end_time for h in open_hours] + [a.end_time for a in appointments]
    if not starts:
        return _WEEK_DEFAULT_RANGE_START, _WEEK_DEFAULT_RANGE_END
    return _round_hour_floor(min(starts)), _round_hour_ceil(max(ends))


def _layout_day_events(events):
    """Distribui atendimentos que se sobrepõem em colunas lado a lado (mesmo
    algoritmo de "cluster + first-fit" usado por calendários no estilo
    Google Calendar) — cada eventos ganha `left_pct`/`width_pct`. Sem
    sobreposição (visão de 1 funcionário só, que nunca tem 2 atendimentos ao
    mesmo tempo), cada evento ocupa a coluna inteira."""
    events = sorted(events, key=lambda e: e["start_minutes"])
    clusters = []
    current_cluster = []
    current_max_end = None
    for e in events:
        if current_cluster and e["start_minutes"] >= current_max_end:
            clusters.append(current_cluster)
            current_cluster = []
            current_max_end = None
        current_cluster.append(e)
        current_max_end = e["end_minutes"] if current_max_end is None else max(current_max_end, e["end_minutes"])
    if current_cluster:
        clusters.append(current_cluster)

    for cluster in clusters:
        columns = []
        for e in cluster:
            placed = False
            for idx, col_end in enumerate(columns):
                if e["start_minutes"] >= col_end:
                    columns[idx] = e["end_minutes"]
                    e["_col"] = idx
                    placed = True
                    break
            if not placed:
                columns.append(e["end_minutes"])
                e["_col"] = len(columns) - 1
        total_cols = len(columns)
        for e in cluster:
            e["width_pct"] = _px(100 / total_cols)
            e["left_pct"] = _px(e["_col"] * (100 / total_cols))
    return events


def _week_grid_context(request, week_start, employee_id):
    week_end = week_start + datetime.timedelta(days=6)
    appointments = list(
        Appointment.objects.for_tenant(request.tenant)
        .filter(date__range=(week_start, week_end))
        .select_related("client", "employee", "service")
        .order_by("date", "start_time")
    )
    if employee_id:
        appointments = [a for a in appointments if str(a.employee_id) == str(employee_id)]

    range_start, range_end = _week_time_range(request.tenant, appointments)
    range_start_minutes = _minutes(range_start)
    range_end_minutes = _minutes(range_end)
    grid_height_px = _px((range_end_minutes - range_start_minutes) * WEEK_PX_PER_MINUTE)

    hours = []
    hour = range_start.hour
    while hour <= range_end.hour:
        hours.append(
            {
                "label": f"{hour:02d}:00",
                "top_px": _px((hour * 60 - range_start_minutes) * WEEK_PX_PER_MINUTE),
            }
        )
        hour += 1

    today = datetime.date.today()
    show_employee_name = not employee_id
    days = []
    for offset in range(7):
        day = week_start + datetime.timedelta(days=offset)
        day_events = []
        for appt in appointments:
            if appt.date != day:
                continue
            start_minutes = _minutes(appt.start_time)
            end_minutes = _minutes(appt.end_time)
            height_px = max(
                (end_minutes - start_minutes) * WEEK_PX_PER_MINUTE, _WEEK_MIN_EVENT_HEIGHT_PX
            )
            day_events.append(
                {
                    "appointment": appt,
                    "start_minutes": start_minutes,
                    "end_minutes": end_minutes,
                    "top_px": _px((start_minutes - range_start_minutes) * WEEK_PX_PER_MINUTE),
                    "height_px": _px(height_px),
                    "chip_class": _STATUS_CHIP_CLASSES.get(appt.status, _STATUS_CHIP_CLASSES["pending"]),
                }
            )
        is_today = day == today
        now_offset_px = None
        if is_today:
            now_time = datetime.datetime.now().time()
            if range_start <= now_time <= range_end:
                now_offset_px = _px((_minutes(now_time) - range_start_minutes) * WEEK_PX_PER_MINUTE)
        days.append(
            {
                "date": day,
                "label": _WEEKDAY_LABELS[offset],
                "is_today": is_today,
                "now_offset_px": now_offset_px,
                "events": _layout_day_events(day_events),
            }
        )

    return {
        "week_start": week_start,
        "week_end": week_end,
        "prev_week": week_start - datetime.timedelta(days=7),
        "next_week": week_start + datetime.timedelta(days=7),
        "hours": hours,
        "grid_height_px": grid_height_px,
        "days": days,
        "show_employee_name": show_employee_name,
        "selected_employee_id": employee_id,
    }


def _week_response(request, week_start, employee_id):
    context = _week_grid_context(request, week_start, employee_id)
    grid = render_to_string("painel/scheduling/_week_grid.html", context, request=request)
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(grid + modal_reset)


@tenant_admin_required
def agenda_week(request):
    any_date = _parse_date(request.GET.get("week"))
    week_start = _week_start_for(any_date)
    employee_id = _employee_filter_id(request)
    context = _week_grid_context(request, week_start, employee_id)
    context.update(
        {
            "employees": Employee.objects.for_tenant(request.tenant).filter(is_active=True).order_by("full_name"),
            "active_nav": "agenda",
        }
    )
    return render(request, "painel/scheduling/week.html", context)


@tenant_admin_required
def appointment_detail(request, pk):
    """Modal aberto ao clicar num evento da grade semanal — mesmas ações de
    status da visão diária, devolvendo pra grade semanal (não pra lista de
    um dia só) depois de qualquer mudança."""
    appointment = _get_appointment(request, pk)
    week_start = _week_start_for(_parse_date(request.GET.get("week")))
    employee_id = _employee_filter_id(request)
    return_qs = f"view=week&week={week_start.isoformat()}&employee={employee_id}"
    return render(
        request,
        "painel/scheduling/_appointment_detail.html",
        {"appointment": appointment, "return_qs": return_qs},
    )


def _agenda_action_response(request):
    """Devolve o HTMX pro lugar de onde a ação partiu — lista de um dia
    (`#agenda-items`) ou grade semanal (`#agenda-week-grid`), conforme o
    parâmetro `view` presente na query string do botão que disparou."""
    if request.GET.get("view") == "week":
        week_start = _week_start_for(_parse_date(request.GET.get("week")))
        employee_id = _employee_filter_id(request)
        return _week_response(request, week_start, employee_id)
    date = _parse_date(request.GET.get("date"))
    return _agenda_response(request, date)


def _agenda_return_qs_and_target(request):
    """Mesma decisão de `_agenda_action_response`, mas pros modais de
    confirmação (GET) montarem a URL de ação e o alvo do swap corretos."""
    if request.GET.get("view") == "week":
        week_start = _week_start_for(_parse_date(request.GET.get("week")))
        employee_id = _employee_filter_id(request)
        return f"view=week&week={week_start.isoformat()}&employee={employee_id}", "#agenda-week-grid"
    date = request.GET.get("date", "")
    return f"date={date}", "#agenda-items"


@employee_required
def my_agenda(request):
    """RF12 — só os próprios agendamentos do funcionário logado, somente
    leitura (ações de status continuam exclusivas do admin em /painel/agenda/)."""
    employee = request.user.employee_profile
    date = _parse_date(request.GET.get("date"))
    appointments = (
        Appointment.objects.for_tenant(request.tenant)
        .filter(date=date, employee=employee)
        .select_related("client", "service")
        .order_by("start_time")
    )
    return render(
        request,
        "painel/scheduling/my_agenda.html",
        {
            "appointments": appointments,
            "selected_date": date,
            "prev_date": date - datetime.timedelta(days=1),
            "next_date": date + datetime.timedelta(days=1),
            "active_nav": "my_agenda",
        },
    )


@tenant_admin_required
def appointment_confirm(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    appointment = _get_appointment(request, pk)
    try:
        confirm_appointment(appointment)
    except ValidationError:
        return HttpResponse(status=409)
    return _agenda_action_response(request)


@tenant_admin_required
def appointment_start(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    appointment = _get_appointment(request, pk)
    try:
        start_appointment(appointment)
    except ValidationError:
        return HttpResponse(status=409)
    return _agenda_action_response(request)


@tenant_admin_required
def appointment_cancel_confirm(request, pk):
    appointment = _get_appointment(request, pk)
    return_qs, target = _agenda_return_qs_and_target(request)
    return render(
        request,
        "painel/_confirm_delete.html",
        {
            "title": "Cancelar agendamento",
            "item_label": f"{appointment.service.name} — {appointment.client.name}",
            "warning": "O cliente será avisado que o horário foi cancelado.",
            "delete_url": f"{reverse('scheduling:cancel', args=[appointment.pk])}?{return_qs}",
            "target": target,
        },
    )


@tenant_admin_required
def appointment_cancel(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    appointment = _get_appointment(request, pk)
    try:
        cancel_appointment(appointment)
    except ValidationError:
        return HttpResponse(status=409)
    return _agenda_action_response(request)


@tenant_admin_required
def appointment_no_show_confirm(request, pk):
    appointment = _get_appointment(request, pk)
    return_qs, target = _agenda_return_qs_and_target(request)
    return render(
        request,
        "painel/_confirm_action.html",
        {
            "title": "Marcar não comparecimento",
            "message": f"{appointment.client.name} não compareceu a este horário?",
            "icon": "person_off",
            "confirm_label": "Marcar não compareceu",
            "action_url": f"{reverse('scheduling:no_show', args=[appointment.pk])}?{return_qs}",
            "target": target,
        },
    )


@tenant_admin_required
def appointment_no_show(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    appointment = _get_appointment(request, pk)
    try:
        mark_no_show(appointment)
    except ValidationError:
        return HttpResponse(status=409)
    return _agenda_action_response(request)


def _slots_context(tenant, service_id, employee_id, date_str, selected_time_str=None):
    """Contexto compartilhado entre o modal de novo agendamento e o parcial
    HTMX de horários — mesma lógica de disponibilidade da página pública
    (apps/scheduling/availability.py), pra nunca divergir do que o cliente
    final vê (regra 5 do CLAUDE.md)."""
    service = None
    employee = None
    selected_date = None
    times = []

    if service_id:
        service = Service.objects.for_tenant(tenant).filter(
            pk=service_id, is_active=True
        ).first()
    if employee_id:
        employee = Employee.objects.for_tenant(tenant).filter(
            pk=employee_id, is_active=True
        ).first()
    if date_str:
        try:
            selected_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            selected_date = None

    if service and employee and selected_date:
        times = get_available_slots_for_day(employee, service, selected_date)

    selected_time = None
    if selected_time_str:
        try:
            selected_time = datetime.time.fromisoformat(selected_time_str)
        except ValueError:
            selected_time = None

    return {
        "slots_service": service,
        "slots_employee": employee,
        "slots_date": selected_date,
        "morning_times": [t for t in times if t.hour < 12],
        "afternoon_times": [t for t in times if t.hour >= 12],
        "selected_time": selected_time,
    }


@tenant_admin_required
def new_appointment_slots(request):
    """Parcial HTMX: grade de horários livres, atualizada a cada troca de
    serviço/profissional/data no modal de novo agendamento (encaixe manual)."""
    context = _slots_context(
        request.tenant,
        request.GET.get("service"),
        request.GET.get("employee"),
        request.GET.get("date"),
        request.GET.get("time"),
    )
    return render(request, "painel/scheduling/_new_slots.html", context)


@tenant_admin_required
def appointment_new(request):
    date_param = request.GET.get("date", "")
    if request.method == "POST":
        form = NewAppointmentForm(request.POST, tenant=request.tenant)
        if form.is_valid():
            data = form.cleaned_data
            try:
                client, _created = get_or_create_client(
                    tenant=request.tenant, phone=data["phone"], name=data["name"]
                )
                create_appointment(
                    tenant=request.tenant,
                    client=client,
                    employee=data["employee"],
                    service=data["service"],
                    date=data["date"],
                    start_time=data["time"],
                    created_by=request.user,
                    notes=data["notes"],
                )
            except ValidationError as exc:
                for field, errors in getattr(
                    exc, "message_dict", {"__all__": exc.messages}
                ).items():
                    for msg in errors:
                        form.add_error(field if field in form.fields else None, msg)
            else:
                return _agenda_response(request, data["date"])
        slots_context = _slots_context(
            request.tenant,
            request.POST.get("service"),
            request.POST.get("employee"),
            request.POST.get("date"),
            request.POST.get("time"),
        )
    else:
        initial = {}
        if date_param:
            initial["date"] = date_param
        form = NewAppointmentForm(tenant=request.tenant, initial=initial)
        slots_context = _slots_context(request.tenant, None, None, date_param, None)
    response = render(
        request, "painel/scheduling/_new_form.html", {"form": form, **slots_context}
    )
    if request.method == "POST":
        response.headers["HX-Retarget"] = "#modal-slot"
        response.headers["HX-Reswap"] = "innerHTML"
    return response
