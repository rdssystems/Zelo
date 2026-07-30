"""Motor de disponibilidade de agenda (regra de negócio #5 do CLAUDE.md).

Disponibilidade = jornada do funcionário (`WorkingHours`) − agendamentos
existentes com status pendente/confirmado (`Appointment`) − exceções
(`ScheduleException`, folga/bloqueio pontual).

Esta é a lógica mais sensível do projeto (02-ARQUITETURA.md §6) — toda a
regra fica centralizada aqui. Não duplicar cálculo de slot livre em outro
lugar (view, template, serializer).

Decisão de design (não especificada nos `.md`, definida aqui): os horários
livres retornados são "back-to-back" — cada slot começa exatamente onde o
anterior termina, com duração igual à do serviço (`service.duration_minutes`).
Não geramos slots a cada N minutos fixos; um slot só existe se o serviço
inteiro couber no espaço livre.
"""

import datetime

from apps.employees.models import ScheduleException, WorkingHours

from .models import BLOCKING_STATUSES, Appointment

_ARBITRARY_DATE = datetime.date(2000, 1, 1)


def _add_minutes(t, minutes):
    return (
        datetime.datetime.combine(_ARBITRARY_DATE, t)
        + datetime.timedelta(minutes=minutes)
    ).time()


def _overlaps(start_a, end_a, start_b, end_b):
    """Intervalos [start, end) — encostar nas pontas (ex.: um termina às 10h,
    outro começa às 10h) NÃO é sobreposição."""
    return start_a < end_b and start_b < end_a


def _slots_in_window(window_start, window_end, duration_minutes):
    """Slots back-to-back que cabem inteiros em [window_start, window_end)."""
    slots = []
    cursor = window_start
    while True:
        slot_end = _add_minutes(cursor, duration_minutes)
        if slot_end > window_end:
            break
        slots.append((cursor, slot_end))
        cursor = slot_end
    return slots


def get_available_slots(employee, service, start_date, end_date):
    """Retorna `{date: [time, ...]}` com os horários de INÍCIO disponíveis
    para agendar `service` com `employee`, entre `start_date` e `end_date`
    (ambos inclusive). Dias sem nenhum horário livre não aparecem no dict.
    """
    if start_date > end_date:
        raise ValueError("start_date não pode ser depois de end_date.")

    duration_minutes = service.duration_minutes

    working_hours_by_weekday = {}
    for wh in WorkingHours.objects.for_tenant(employee.tenant).filter(
        employee=employee, is_active=True
    ):
        working_hours_by_weekday.setdefault(wh.weekday, []).append(
            (wh.start_time, wh.end_time)
        )

    booked_by_date = {}
    for appt in Appointment.objects.for_tenant(employee.tenant).filter(
        employee=employee,
        date__range=(start_date, end_date),
        status__in=BLOCKING_STATUSES,
    ):
        booked_by_date.setdefault(appt.date, []).append(
            (appt.start_time, appt.end_time)
        )

    exceptions_by_date = {}
    for exc in ScheduleException.objects.for_tenant(employee.tenant).filter(
        employee=employee, date__range=(start_date, end_date)
    ):
        exceptions_by_date.setdefault(exc.date, []).append(
            (exc.start_time, exc.end_time)
        )

    result = {}
    current = start_date
    one_day = datetime.timedelta(days=1)
    while current <= end_date:
        day_exceptions = exceptions_by_date.get(current, [])
        if any(start is None for start, end in day_exceptions):
            current += one_day
            continue  # exceção de dia inteiro: nenhum slot possível

        day_slots = []
        for window_start, window_end in working_hours_by_weekday.get(
            current.weekday(), []
        ):
            day_slots.extend(
                _slots_in_window(window_start, window_end, duration_minutes)
            )

        blockers = day_exceptions + booked_by_date.get(current, [])
        free_slots = [
            slot_start
            for slot_start, slot_end in day_slots
            if not any(
                _overlaps(slot_start, slot_end, block_start, block_end)
                for block_start, block_end in blockers
            )
        ]

        if free_slots:
            result[current] = free_slots
        current += one_day

    return result


def compute_end_time(start_time, duration_minutes):
    """Horário de término dado o início e a duração do serviço (minutos)."""
    return _add_minutes(start_time, duration_minutes)


def get_available_slots_for_day(employee, service, day):
    """Atalho de `get_available_slots` para um único dia."""
    return get_available_slots(employee, service, day, day).get(day, [])


def is_slot_available(employee, service, day, start_time):
    """Revalida um horário específico no momento da confirmação do
    agendamento (evita condição de corrida entre a consulta de disponibilidade
    e a criação do `Appointment`, ambos na Etapa 5)."""
    return start_time in get_available_slots_for_day(employee, service, day)
