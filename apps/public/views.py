import datetime
from urllib.parse import quote

from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django_ratelimit.decorators import ratelimit

from apps.clients.models import Client
from apps.clients.services import anonymize_client, get_or_create_client, normalize_phone
from apps.employees.models import Employee, EmployeeService
from apps.scheduling.availability import get_available_slots, is_slot_available
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    cancel_appointment,
    create_appointment,
    upcoming_appointments_for_client,
)
from apps.services.services import bookable_services
from apps.tenants.models import TenantBusinessHours

SCHEDULE_WINDOW_DAYS = 14

BIRTH_MONTH_CHOICES = [
    (1, "Jan"), (2, "Fev"), (3, "Mar"), (4, "Abr"), (5, "Mai"), (6, "Jun"),
    (7, "Jul"), (8, "Ago"), (9, "Set"), (10, "Out"), (11, "Nov"), (12, "Dez"),
]


# ---------------------------------------------------------------------------
# Helpers de validação — cada etapa depende da(s) anterior(es).
# ---------------------------------------------------------------------------


def _get_bookable_service(tenant, service_id):
    return get_object_or_404(bookable_services(tenant), pk=service_id)


def _get_linked_active_employee(tenant, service, employee_id):
    employee = get_object_or_404(
        Employee.objects.for_tenant(tenant).filter(is_active=True), pk=employee_id
    )
    if not EmployeeService.objects.filter(employee=employee, service=service).exists():
        raise Http404("Este profissional não atende este serviço.")
    return employee


def _parse_date(raw):
    try:
        return datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise Http404("Data inválida.")


def _parse_time(raw):
    try:
        return datetime.time.fromisoformat(raw)
    except (TypeError, ValueError):
        raise Http404("Horário inválido.")


def _session_key(tenant, name):
    return f"public_{tenant.slug}_{name}"


# ---------------------------------------------------------------------------
# Página inicial do salão (RF01, RF02)
# ---------------------------------------------------------------------------


def salon_home(request, tenant_slug):
    tenant = request.tenant
    today_weekday = datetime.date.today().weekday()
    business_hours = [
        {
            "label": h.get_weekday_display(),
            "is_closed": h.is_closed,
            "start_time": h.start_time,
            "end_time": h.end_time,
            "is_today": h.weekday == today_weekday,
        }
        for h in TenantBusinessHours.objects.for_tenant(tenant).order_by("weekday")
    ]
    today_hours = next((day for day in business_hours if day["is_today"]), None)
    if today_hours is None:
        today_label = ""
    elif today_hours["is_closed"]:
        today_label = "Fechado hoje"
    else:
        today_label = (
            f"Hoje: {today_hours['start_time']:%H:%M} – {today_hours['end_time']:%H:%M}"
        )
    return render(
        request,
        "public/home.html",
        {"tenant": tenant, "business_hours": business_hours, "today_hours_label": today_label},
    )


# ---------------------------------------------------------------------------
# Fluxo de agendamento (RF03)
# ---------------------------------------------------------------------------


def booking_service(request, tenant_slug):
    services = bookable_services(request.tenant)
    return render(
        request, "public/booking/service.html", {"tenant": request.tenant, "services": services}
    )


def booking_employee(request, tenant_slug):
    service = _get_bookable_service(request.tenant, request.GET.get("service"))
    employees = Employee.objects.for_tenant(request.tenant).filter(
        is_active=True, service_links__service=service
    )
    return render(
        request,
        "public/booking/employee.html",
        {"tenant": request.tenant, "service": service, "employees": employees},
    )


def _schedule_context(request, tenant):
    service = _get_bookable_service(tenant, request.GET.get("service"))
    employee = _get_linked_active_employee(tenant, service, request.GET.get("employee"))

    today = datetime.date.today()
    end_date = today + datetime.timedelta(days=SCHEDULE_WINDOW_DAYS - 1)
    slots_by_date = get_available_slots(employee, service, today, end_date)

    raw_date = request.GET.get("date")
    if raw_date:
        selected_date = _parse_date(raw_date)
    else:
        selected_date = next(iter(slots_by_date), None)

    dates = [
        {"date": today + datetime.timedelta(days=i), "has_slots": (today + datetime.timedelta(days=i)) in slots_by_date}
        for i in range(SCHEDULE_WINDOW_DAYS)
    ]
    times = slots_by_date.get(selected_date, []) if selected_date else []
    return {
        "tenant": tenant,
        "service": service,
        "employee": employee,
        "dates": dates,
        "selected_date": selected_date,
        "morning_times": [t for t in times if t.hour < 12],
        "afternoon_times": [t for t in times if t.hour >= 12],
    }


def booking_schedule(request, tenant_slug):
    context = _schedule_context(request, request.tenant)
    return render(request, "public/booking/schedule.html", context)


def booking_schedule_slots(request, tenant_slug):
    """Partial HTMX: só a grade de horários, ao trocar a data selecionada."""
    context = _schedule_context(request, request.tenant)
    return render(request, "public/booking/_time_slots.html", context)


def booking_identify(request, tenant_slug):
    tenant = request.tenant
    service = _get_bookable_service(tenant, request.GET.get("service"))
    employee = _get_linked_active_employee(tenant, service, request.GET.get("employee"))
    date = _parse_date(request.GET.get("date"))
    start_time = _parse_time(request.GET.get("time"))

    booking_qs = (
        f"service={service.pk}&employee={employee.pk}"
        f"&date={date.isoformat()}&time={start_time.isoformat()}"
    )

    if not is_slot_available(employee, service, date, start_time):
        return render(
            request,
            "public/booking/slot_taken.html",
            {"tenant": tenant, "service": service, "employee": employee},
        )

    error = None
    show_name_field = False
    phone_value = ""
    name_value = ""
    birth_day_value = ""
    birth_month_value = ""

    if request.method == "POST":
        phone_value = request.POST.get("phone", "")
        name_value = request.POST.get("name", "")
        birth_day_value = request.POST.get("birth_day", "")
        birth_month_value = request.POST.get("birth_month", "")
        try:
            client, created = get_or_create_client(
                tenant=tenant, phone=phone_value, name=name_value,
                birth_day=birth_day_value or None, birth_month=birth_month_value or None,
            )
        except ValidationError as exc:
            if hasattr(exc, "message_dict") and "name" in exc.message_dict:
                # telefone válido e novo — só falta o nome (RF04)
                show_name_field = True
            elif hasattr(exc, "message_dict") and "birth_day" in exc.message_dict:
                show_name_field = True
                error = " ".join(exc.message_dict["birth_day"])
            else:
                error = " ".join(exc.messages)
        else:
            confirm_url = (
                reverse("public:booking_confirm", args=[tenant_slug])
                + f"?{booking_qs}&client={client.pk}"
            )
            return redirect(confirm_url)

    return render(
        request,
        "public/booking/identify.html",
        {
            "tenant": tenant,
            "service": service,
            "employee": employee,
            "date": date,
            "start_time": start_time,
            "booking_qs": booking_qs,
            "show_name_field": show_name_field,
            "phone_value": phone_value,
            "name_value": name_value,
            "birth_day_value": birth_day_value,
            "birth_month_value": birth_month_value,
            "day_range": range(1, 32),
            "month_choices": BIRTH_MONTH_CHOICES,
            "error": error,
        },
    )


@ratelimit(key="ip", rate="20/h", method="POST", block=False)
@ratelimit(key="post:phone", rate="5/h", method="POST", block=False)
def booking_confirm(request, tenant_slug):
    tenant = request.tenant
    service = _get_bookable_service(tenant, request.GET.get("service"))
    employee = _get_linked_active_employee(tenant, service, request.GET.get("employee"))
    date = _parse_date(request.GET.get("date"))
    start_time = _parse_time(request.GET.get("time"))
    client = get_object_or_404(
        Client.objects.for_tenant(tenant), pk=request.GET.get("client")
    )

    error = None
    if request.method == "POST":
        if getattr(request, "limited", False):
            error = "Muitas tentativas. Aguarde alguns minutos e tente novamente."
        else:
            try:
                appointment = create_appointment(
                    tenant=tenant,
                    client=client,
                    employee=employee,
                    service=service,
                    date=date,
                    start_time=start_time,
                )
            except ValidationError as exc:
                error = " ".join(exc.messages)
            else:
                return redirect("public:booking_success", tenant_slug, appointment.pk)

    return render(
        request,
        "public/booking/confirm.html",
        {
            "tenant": tenant,
            "service": service,
            "employee": employee,
            "date": date,
            "start_time": start_time,
            "client": client,
            "error": error,
        },
    )


def booking_success(request, tenant_slug, appointment_id):
    appointment = get_object_or_404(
        Appointment.objects.for_tenant(request.tenant).select_related(
            "employee", "service", "client"
        ),
        pk=appointment_id,
    )
    return render(
        request, "public/booking/success.html", {"tenant": request.tenant, "appointment": appointment}
    )


# ---------------------------------------------------------------------------
# RF06 — cliente vê/cancela agendamento futuro reentrando com o telefone
# ---------------------------------------------------------------------------


def my_appointments(request, tenant_slug):
    tenant = request.tenant
    error = None
    client = None
    appointments = None

    if request.method == "POST":
        try:
            phone = normalize_phone(request.POST.get("phone", ""))
            client = Client.objects.for_tenant(tenant).get(phone=phone)
        except ValidationError as exc:
            error = " ".join(exc.messages)
        except Client.DoesNotExist:
            error = "Não encontramos nenhum agendamento com esse telefone."
        else:
            request.session[_session_key(tenant, "verified_client")] = client.pk
            appointments = upcoming_appointments_for_client(client)

    return render(
        request,
        "public/my_appointments.html",
        {"tenant": tenant, "error": error, "client": client, "appointments": appointments},
    )


def _get_verified_appointment(request, tenant, appointment_id):
    verified_client_id = request.session.get(_session_key(tenant, "verified_client"))
    appointment = get_object_or_404(
        Appointment.objects.for_tenant(tenant).select_related("employee", "service"),
        pk=appointment_id,
    )
    if verified_client_id != appointment.client_id:
        raise Http404("Agendamento não encontrado.")
    return appointment


def _cancel_whatsapp_url(tenant, appointment):
    """RF06c: cliente cancelando pela página pública é redirecionado pro
    WhatsApp do salão com um aviso pronto (editável dentro do próprio
    WhatsApp) — `None` se o salão desligou a opção em Configurações ou não
    tem WhatsApp cadastrado."""
    if not tenant.whatsapp_cancel_redirect_enabled:
        return None
    number = tenant.whatsapp_wa_me_number
    if not number:
        return None
    message = (
        f"Olá! Sou {appointment.client.name} e decidi cancelar meu agendamento de "
        f"{appointment.service.name} marcado para {appointment.date.strftime('%d/%m/%Y')} "
        f"às {appointment.start_time.strftime('%H:%M')}. Só avisando por aqui!"
    )
    return f"https://wa.me/{number}?text={quote(message)}"


def cancel_appointment_confirm(request, tenant_slug, appointment_id):
    appointment = _get_verified_appointment(request, request.tenant, appointment_id)
    return render(
        request,
        "public/_confirm_cancel.html",
        {
            "tenant": request.tenant,
            "appointment": appointment,
            "action_url": reverse(
                "public:cancel_appointment", args=[tenant_slug, appointment.pk]
            ),
            "cancel_whatsapp_url": _cancel_whatsapp_url(request.tenant, appointment),
        },
    )


def cancel_appointment_view(request, tenant_slug, appointment_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    appointment = _get_verified_appointment(request, request.tenant, appointment_id)
    try:
        cancel_appointment(appointment, canceled_by_client=True)
    except ValidationError:
        pass
    appointments = upcoming_appointments_for_client(appointment.client)
    html = render_to_string(
        "public/_appointments_list.html",
        {"tenant": request.tenant, "appointments": appointments},
        request=request,
    )
    modal_reset = '<div id="modal-slot" hx-swap-oob="true"></div>'
    return HttpResponse(html + modal_reset)


def _get_verified_client(request, tenant):
    client_id = request.session.get(_session_key(tenant, "verified_client"))
    if not client_id:
        raise Http404("Verifique seu telefone antes de continuar.")
    return get_object_or_404(Client.objects.for_tenant(tenant), pk=client_id)


def delete_my_data_confirm(request, tenant_slug):
    """RNF07 — modal de confirmação (mesmo padrão do app, sem confirm() nativo)."""
    tenant = request.tenant
    client = _get_verified_client(request, tenant)
    return render(
        request,
        "public/_confirm_delete_data.html",
        {
            "tenant": tenant,
            "client": client,
            "action_url": reverse("public:delete_my_data", args=[tenant_slug]),
        },
    )


def delete_my_data_view(request, tenant_slug):
    if request.method != "POST":
        return HttpResponse(status=405)
    tenant = request.tenant
    client = _get_verified_client(request, tenant)
    anonymize_client(client)
    del request.session[_session_key(tenant, "verified_client")]
    return render(request, "public/data_deleted.html", {"tenant": tenant})
