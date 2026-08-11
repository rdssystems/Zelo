import datetime
import unittest.mock
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.clients.models import Client
from apps.employees.models import ScheduleException, WorkingHours
from apps.employees.services import create_employee, link_service, set_working_hours
from apps.services.services import add_recipe_item, create_service, delete_service, set_service_active
from apps.tenants.models import Tenant

from apps.finance.models import CashCategory, CashFlowType, CashTransaction, Commission, CommissionStatus
from apps.inventory.models import MovementReason, Product, StockMovement
from apps.inventory.services import create_product

from .availability import get_available_slots, get_available_slots_for_day, is_slot_available
from .models import Appointment, AppointmentStatus
from .services import (
    build_product_usage,
    cancel_appointment,
    complete_appointment,
    complete_client_comanda,
    confirm_appointment,
    create_appointment,
    mark_no_show,
    remove_appointment_from_comanda,
    start_walk_in_service,
    upcoming_appointments_for_client,
)

User = get_user_model()


def make_tenant_with_admin(slug):
    tenant = Tenant.objects.create(name=f"Salão {slug}", slug=slug)
    admin = User.objects.create_user(
        email=f"admin@{slug}.com",
        password="x",
        role=User.Role.TENANT_ADMIN,
        tenant=tenant,
    )
    return tenant, admin


def make_employee(tenant, email="ana@salao.com", full_name="Ana Silva"):
    return create_employee(
        tenant=tenant,
        full_name=full_name,
        email=email,
        password="Senha@123",
        default_commission_type="percentage",
        default_commission_value=Decimal("40.00"),
    )


def make_client(tenant, phone="+5511999990000", name="Cliente Teste"):
    return Client.objects.create(tenant=tenant, phone=phone, name=name)


def next_weekday(start, weekday):
    """Primeira data >= start cujo weekday() == weekday (0=Segunda...6=Domingo)."""
    days_ahead = (weekday - start.weekday()) % 7
    return start + datetime.timedelta(days=days_ahead)


def book(tenant, employee, service, client, date, start_time, end_time, status=AppointmentStatus.CONFIRMED):
    return Appointment.objects.create(
        tenant=tenant,
        client=client,
        employee=employee,
        service=service,
        date=date,
        start_time=start_time,
        end_time=end_time,
        status=status,
        price_at_booking=service.price,
    )


class AppointmentModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        cls.client_ = make_client(cls.tenant)

    def test_end_before_start_rejected_by_db_constraint(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            book(
                self.tenant,
                self.employee,
                self.service,
                self.client_,
                datetime.date(2026, 8, 3),
                datetime.time(10, 0),
                datetime.time(9, 0),
            )

    def test_isolation(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        other_employee = make_employee(
            other_tenant, email="bia@salao-b.com", full_name="Bia"
        )
        other_service = create_service(
            tenant=other_tenant, name="Corte B", duration_minutes=60, price=Decimal("50")
        )
        other_client = make_client(other_tenant, phone="+5511888880000", name="Outro")
        book(
            self.tenant, self.employee, self.service, self.client_,
            datetime.date(2026, 8, 3), datetime.time(9, 0), datetime.time(10, 0),
        )
        book(
            other_tenant, other_employee, other_service, other_client,
            datetime.date(2026, 8, 3), datetime.time(9, 0), datetime.time(10, 0),
        )
        self.assertEqual(
            Appointment.objects.for_tenant(self.tenant).count(), 1
        )

    def test_cannot_delete_service_with_appointment(self):
        book(
            self.tenant, self.employee, self.service, self.client_,
            datetime.date(2026, 8, 3), datetime.time(9, 0), datetime.time(10, 0),
        )
        set_service_active(self.service, False)
        with self.assertRaises(ValidationError):
            delete_service(self.service)
        self.assertTrue(type(self.service).objects.filter(pk=self.service.pk).exists())


class AvailabilityBasicTest(TestCase):
    """Jornada simples de um dia, sem agendamentos nem exceções."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)
        set_working_hours(
            cls.employee,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}],
        )

    def test_hourly_service_fills_the_day_back_to_back(self):
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        slots = get_available_slots(self.employee, service, self.monday, self.monday)
        self.assertEqual(
            slots[self.monday],
            [datetime.time(h) for h in range(9, 18)],
        )

    def test_duration_dividing_window_evenly(self):
        service = create_service(
            tenant=self.tenant, name="Coloração", duration_minutes=90, price=Decimal("100")
        )
        slots = get_available_slots(self.employee, service, self.monday, self.monday)
        self.assertEqual(
            slots[self.monday],
            [
                datetime.time(9, 0), datetime.time(10, 30), datetime.time(12, 0),
                datetime.time(13, 30), datetime.time(15, 0), datetime.time(16, 30),
            ],
        )

    def test_day_without_working_hours_has_no_slots(self):
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        tuesday = self.monday + datetime.timedelta(days=1)
        slots = get_available_slots(self.employee, service, tuesday, tuesday)
        self.assertEqual(slots, {})

    def test_service_duration_that_does_not_fit_leftover_time(self):
        """Janela de 90min com serviço de 60min: só cabe 1 slot, sobram 30min inutilizáveis."""
        set_working_hours(
            self.employee,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(10, 30)}],
        )
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        slots = get_available_slots(self.employee, service, self.monday, self.monday)
        self.assertEqual(slots[self.monday], [datetime.time(9, 0)])

    def test_start_date_after_end_date_raises(self):
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        with self.assertRaises(ValueError):
            get_available_slots(
                self.employee, service, self.monday, self.monday - datetime.timedelta(days=1)
            )

    def test_inactive_working_hours_ignored(self):
        WorkingHours.objects.filter(employee=self.employee).update(is_active=False)
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        slots = get_available_slots(self.employee, service, self.monday, self.monday)
        self.assertEqual(slots, {})


class AvailabilitySplitShiftTest(TestCase):
    """Jornada com múltiplas janelas no mesmo dia (turno partido)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)
        # set_working_hours substitui a jornada inteira — para 2 janelas no
        # mesmo dia, criamos direto via ORM (o painel só permite 1 faixa/dia
        # hoje; o motor de disponibilidade já suporta múltiplas por robustez)
        WorkingHours.objects.create(
            tenant=cls.tenant, employee=cls.employee, weekday=0,
            start_time=datetime.time(9, 0), end_time=datetime.time(12, 0),
        )
        WorkingHours.objects.create(
            tenant=cls.tenant, employee=cls.employee, weekday=0,
            start_time=datetime.time(14, 0), end_time=datetime.time(18, 0),
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )

    def test_slots_do_not_bridge_the_gap_between_windows(self):
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        expected = [
            datetime.time(9, 0), datetime.time(10, 0), datetime.time(11, 0),
            datetime.time(14, 0), datetime.time(15, 0), datetime.time(16, 0), datetime.time(17, 0),
        ]
        self.assertEqual(slots[self.monday], expected)
        self.assertNotIn(datetime.time(12, 0), slots[self.monday])
        self.assertNotIn(datetime.time(13, 0), slots[self.monday])


class AvailabilityRecurringBreakTest(TestCase):
    """2º turno em `WorkingHours` (start_time_2/end_time_2) — pausa
    recorrente todo dia (ex. almoço), decisão do usuário em 2026-08-06.
    Diferente de `AvailabilitySplitShiftTest` (que usa 2 linhas separadas
    via ORM direto): aqui é 1 linha só, pelo caminho real do painel
    (`set_working_hours`)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )

    def test_break_splits_the_day_in_two_windows(self):
        set_working_hours(
            self.employee,
            [{
                "weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(12, 0),
                "start_time_2": datetime.time(13, 0), "end_time_2": datetime.time(18, 0),
            }],
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertNotIn(datetime.time(12, 0), slots[self.monday])
        self.assertIn(datetime.time(9, 0), slots[self.monday])
        self.assertIn(datetime.time(13, 0), slots[self.monday])

    def test_only_one_side_of_break_is_rejected(self):
        with self.assertRaises(ValidationError):
            set_working_hours(
                self.employee,
                [{
                    "weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0),
                    "start_time_2": datetime.time(13, 0), "end_time_2": None,
                }],
            )

    def test_break_before_first_shift_ends_is_rejected(self):
        with self.assertRaises(ValidationError):
            set_working_hours(
                self.employee,
                [{
                    "weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0),
                    "start_time_2": datetime.time(11, 0), "end_time_2": datetime.time(12, 0),
                }],
            )

    def test_break_end_before_break_start_is_rejected(self):
        with self.assertRaises(ValidationError):
            set_working_hours(
                self.employee,
                [{
                    "weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(12, 0),
                    "start_time_2": datetime.time(14, 0), "end_time_2": datetime.time(13, 0),
                }],
            )

    def test_no_break_still_works_as_before(self):
        """`start_time_2`/`end_time_2` nem sequer precisam vir no dict —
        compatibilidade com todo o resto da suíte que já chama
        `set_working_hours` sem eles."""
        set_working_hours(
            self.employee,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}],
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertIn(datetime.time(12, 0), slots[self.monday])


class AvailabilityPastSlotCutoffTest(TestCase):
    """Horário de hoje que já passou não pode mais ser agendado (decisão do
    usuário em 2026-08-06) — corta exatamente quando bate o horário, sem
    antecedência mínima. Dias futuros não são afetados."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )

    def _set_hours_for_weekday(self, weekday):
        set_working_hours(
            self.employee,
            [{"weekday": weekday, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}],
        )

    def test_slots_before_now_hidden_today(self):
        today = datetime.date(2026, 8, 6)  # quinta-feira
        self._set_hours_for_weekday(today.weekday())
        with unittest.mock.patch("apps.scheduling.availability.timezone") as mock_tz:
            mock_tz.localdate.return_value = today
            mock_tz.localtime.return_value = datetime.datetime.combine(today, datetime.time(15, 0))
            slots = get_available_slots(self.employee, self.service, today, today)
        self.assertNotIn(datetime.time(9, 0), slots[today])
        self.assertNotIn(datetime.time(14, 0), slots[today])
        # 15h é o próprio "agora" mockado — já corta (ver
        # test_slot_at_exact_current_time_is_cut), só o que vem DEPOIS sobra.
        self.assertNotIn(datetime.time(15, 0), slots[today])
        self.assertIn(datetime.time(16, 0), slots[today])
        self.assertIn(datetime.time(17, 0), slots[today])

    def test_slot_at_exact_current_time_is_cut(self):
        """"Corta quando dá o horário" — às 15:00 em ponto, o slot das 15:00
        já não aparece mais (não só a partir de 15:01)."""
        today = datetime.date(2026, 8, 6)
        self._set_hours_for_weekday(today.weekday())
        with unittest.mock.patch("apps.scheduling.availability.timezone") as mock_tz:
            mock_tz.localdate.return_value = today
            mock_tz.localtime.return_value = datetime.datetime.combine(today, datetime.time(15, 0))
            slots = get_available_slots(self.employee, self.service, today, today)
        self.assertNotIn(datetime.time(15, 0), slots[today])
        self.assertIn(datetime.time(16, 0), slots[today])

    def test_future_day_not_affected_by_current_time(self):
        today = datetime.date(2026, 8, 6)
        tomorrow = today + datetime.timedelta(days=1)
        self._set_hours_for_weekday(tomorrow.weekday())
        with unittest.mock.patch("apps.scheduling.availability.timezone") as mock_tz:
            mock_tz.localdate.return_value = today
            mock_tz.localtime.return_value = datetime.datetime.combine(today, datetime.time(23, 0))
            slots = get_available_slots(self.employee, self.service, tomorrow, tomorrow)
        self.assertIn(datetime.time(9, 0), slots[tomorrow])


class AvailabilityBookedAppointmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)
        set_working_hours(
            cls.employee,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}],
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )

    def test_pending_appointment_blocks_its_slot(self):
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(10, 0), datetime.time(11, 0),
            status=AppointmentStatus.PENDING,
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertNotIn(datetime.time(10, 0), slots[self.monday])
        self.assertEqual(len(slots[self.monday]), 8)

    def test_back_to_back_appointment_does_not_block_adjacent_slots(self):
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(10, 0), datetime.time(11, 0),
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertIn(datetime.time(9, 0), slots[self.monday])
        self.assertIn(datetime.time(11, 0), slots[self.monday])

    def test_confirmed_appointment_blocks_slot(self):
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(10, 0), datetime.time(11, 0),
            status=AppointmentStatus.CONFIRMED,
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertNotIn(datetime.time(10, 0), slots[self.monday])

    def test_completed_appointment_does_not_block_slot(self):
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(10, 0), datetime.time(11, 0),
            status=AppointmentStatus.COMPLETED,
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertIn(datetime.time(10, 0), slots[self.monday])

    def test_canceled_appointment_does_not_block_slot(self):
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(10, 0), datetime.time(11, 0),
            status=AppointmentStatus.CANCELED,
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertIn(datetime.time(10, 0), slots[self.monday])

    def test_no_show_appointment_does_not_block_slot(self):
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(10, 0), datetime.time(11, 0),
            status=AppointmentStatus.NO_SHOW,
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertIn(datetime.time(10, 0), slots[self.monday])

    def test_appointment_crossing_end_of_shift_removes_only_overlapping_slots(self):
        """Encaixe manual (RF17) que ultrapassa o fim do expediente não deve
        quebrar o cálculo, só remover os slots que colidem com ele."""
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(17, 30), datetime.time(18, 30),
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertNotIn(datetime.time(17, 0), slots[self.monday])
        self.assertIn(datetime.time(16, 0), slots[self.monday])
        self.assertEqual(len(slots[self.monday]), 8)


class AvailabilityExceptionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)
        set_working_hours(
            cls.employee,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}],
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )

    def test_full_day_exception_removes_the_whole_day(self):
        ScheduleException.objects.create(
            tenant=self.tenant, employee=self.employee, date=self.monday, reason="Folga",
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertNotIn(self.monday, slots)

    def test_partial_exception_removes_only_overlapping_slots(self):
        ScheduleException.objects.create(
            tenant=self.tenant, employee=self.employee, date=self.monday,
            start_time=datetime.time(12, 0), end_time=datetime.time(13, 0),
            reason="Almoço",
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertNotIn(datetime.time(12, 0), slots[self.monday])
        self.assertIn(datetime.time(11, 0), slots[self.monday])
        self.assertIn(datetime.time(13, 0), slots[self.monday])
        self.assertEqual(len(slots[self.monday]), 8)

    def test_exception_on_other_date_does_not_affect_this_day(self):
        tuesday = self.monday + datetime.timedelta(days=1)
        ScheduleException.objects.create(
            tenant=self.tenant, employee=self.employee, date=tuesday, reason="Folga",
        )
        slots = get_available_slots(self.employee, self.service, self.monday, self.monday)
        self.assertEqual(len(slots[self.monday]), 9)


class AvailabilityMultiDayRangeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        # Âncora 14 dias à frente de "hoje" (não uma data fixa) — desde que
        # get_available_slots passou a cortar horário passado de hoje
        # (2026-08-06), uma data fixa podia coincidir com o "hoje" de
        # verdade no momento em que a suíte roda e quebrar o teste à toa.
        cls.monday = next_weekday(datetime.date.today() + datetime.timedelta(days=14), 0)
        cls.wednesday = cls.monday + datetime.timedelta(days=2)
        # jornada só em segunda e quarta
        set_working_hours(
            cls.employee,
            [
                {"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(12, 0)},
                {"weekday": 2, "start_time": datetime.time(9, 0), "end_time": datetime.time(12, 0)},
            ],
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )

    def test_only_configured_weekdays_appear_in_range(self):
        sunday_before = self.monday - datetime.timedelta(days=1)
        thursday = self.wednesday + datetime.timedelta(days=1)
        slots = get_available_slots(self.employee, self.service, sunday_before, thursday)
        self.assertEqual(set(slots.keys()), {self.monday, self.wednesday})

    def test_inclusive_boundaries(self):
        slots = get_available_slots(self.employee, self.service, self.monday, self.wednesday)
        self.assertIn(self.monday, slots)
        self.assertIn(self.wednesday, slots)


class AvailabilityEmployeeIsolationTest(TestCase):
    """Agenda de um funcionário não pode vazar/afetar a de outro (regra #1)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.ana = make_employee(cls.tenant, email="ana@salao-a.com", full_name="Ana")
        cls.bia = make_employee(cls.tenant, email="bia@salao-a.com", full_name="Bia")
        cls.client_ = make_client(cls.tenant)
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)
        for employee in (cls.ana, cls.bia):
            set_working_hours(
                employee,
                [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(12, 0)}],
            )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )

    def test_booking_for_one_employee_does_not_affect_another(self):
        book(
            self.tenant, self.ana, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
        )
        ana_slots = get_available_slots(self.ana, self.service, self.monday, self.monday)
        bia_slots = get_available_slots(self.bia, self.service, self.monday, self.monday)
        self.assertNotIn(datetime.time(9, 0), ana_slots[self.monday])
        self.assertIn(datetime.time(9, 0), bia_slots[self.monday])


class AvailabilityHelpersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)
        set_working_hours(
            cls.employee,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(12, 0)}],
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )

    def test_get_available_slots_for_day(self):
        self.assertEqual(
            get_available_slots_for_day(self.employee, self.service, self.monday),
            [datetime.time(9, 0), datetime.time(10, 0), datetime.time(11, 0)],
        )

    def test_get_available_slots_for_day_with_no_slots_returns_empty_list(self):
        tuesday = self.monday + datetime.timedelta(days=1)
        self.assertEqual(
            get_available_slots_for_day(self.employee, self.service, tuesday), []
        )

    def test_is_slot_available_true_and_false(self):
        self.assertTrue(
            is_slot_available(self.employee, self.service, self.monday, datetime.time(9, 0))
        )
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
        )
        self.assertFalse(
            is_slot_available(self.employee, self.service, self.monday, datetime.time(9, 0))
        )


class CreateAppointmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)
        set_working_hours(
            cls.employee,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}],
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )

    def test_creates_pending_appointment_with_price_snapshot(self):
        appointment = create_appointment(
            tenant=self.tenant,
            client=self.client_,
            employee=self.employee,
            service=self.service,
            date=self.monday,
            start_time=datetime.time(9, 0),
        )
        self.assertEqual(appointment.status, AppointmentStatus.PENDING)
        self.assertEqual(appointment.end_time, datetime.time(10, 0))
        self.assertEqual(appointment.price_at_booking, self.service.price)

    def test_auto_confirm_appointments_creates_confirmed(self):
        self.tenant.auto_confirm_appointments = True
        self.tenant.save(update_fields=["auto_confirm_appointments"])
        appointment = create_appointment(
            tenant=self.tenant,
            client=self.client_,
            employee=self.employee,
            service=self.service,
            date=self.monday,
            start_time=datetime.time(9, 0),
        )
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_price_snapshot_survives_later_price_change(self):
        appointment = create_appointment(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.service, date=self.monday, start_time=datetime.time(9, 0),
        )
        from apps.services.services import update_service

        update_service(
            self.service, name=self.service.name, duration_minutes=60,
            price=Decimal("999.00"),
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.price_at_booking, Decimal("100.00"))

    def test_client_booking_creates_tenant_notification(self):
        """`created_by=None` é como a página pública marca "foi o cliente"
        (mesmo critério de `Appointment.created_by`, ver
        03-MODELO-DE-DADOS.md) — decisão do usuário em 2026-08-06, estende
        RF06g pra também notificar agendamento novo, não só cancelamento."""
        from apps.notifications.models import TenantNotification, TenantNotificationKind

        appointment = create_appointment(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.service, date=self.monday, start_time=datetime.time(9, 0),
        )
        notification = TenantNotification.objects.get(tenant=self.tenant)
        self.assertEqual(notification.kind, TenantNotificationKind.APPOINTMENT_CREATED_BY_CLIENT)
        self.assertEqual(notification.appointment_id, appointment.pk)
        self.assertIn(self.client_.name, notification.message)
        self.assertFalse(notification.is_read)

    def test_admin_booking_does_not_create_notification(self):
        """Agendamento criado pelo próprio painel (admin/funcionário
        marcando pro cliente) não precisa notificar quem acabou de agir."""
        from apps.notifications.models import TenantNotification

        create_appointment(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.service, date=self.monday, start_time=datetime.time(9, 0),
            created_by=self.admin,
        )
        self.assertFalse(TenantNotification.objects.filter(tenant=self.tenant).exists())

    def test_rejects_already_taken_slot(self):
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
        )
        with self.assertRaises(ValidationError):
            create_appointment(
                tenant=self.tenant, client=self.client_, employee=self.employee,
                service=self.service, date=self.monday, start_time=datetime.time(9, 0),
            )

    def test_rejects_inactive_employee(self):
        from apps.employees.services import set_employee_active

        set_employee_active(self.employee, False)
        with self.assertRaises(ValidationError):
            create_appointment(
                tenant=self.tenant, client=self.client_, employee=self.employee,
                service=self.service, date=self.monday, start_time=datetime.time(9, 0),
            )

    def test_rejects_inactive_service(self):
        set_service_active(self.service, False)
        with self.assertRaises(ValidationError):
            create_appointment(
                tenant=self.tenant, client=self.client_, employee=self.employee,
                service=self.service, date=self.monday, start_time=datetime.time(9, 0),
            )

    def test_rejects_cross_tenant_client(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        other_client = make_client(other_tenant, phone="+5511888880000", name="Outro")
        with self.assertRaises(ValidationError):
            create_appointment(
                tenant=self.tenant, client=other_client, employee=self.employee,
                service=self.service, date=self.monday, start_time=datetime.time(9, 0),
            )

    def test_unique_constraint_blocks_race_condition_at_db_level(self):
        """Simula a corrida: dois inserts para o mesmo funcionário/dia/hora
        driblando a checagem de disponibilidade (ex.: ambos passaram a
        validação antes de qualquer um commitar)."""
        create_appointment(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.service, date=self.monday, start_time=datetime.time(9, 0),
        )
        second_client = make_client(self.tenant, phone="+5511777770000", name="Segunda")
        with self.assertRaises(IntegrityError), transaction.atomic():
            # bypassa a checagem de disponibilidade chamando o create direto
            # do model, igual uma segunda transação concorrente faria
            Appointment.objects.create(
                tenant=self.tenant, client=second_client, employee=self.employee,
                service=self.service, date=self.monday, start_time=datetime.time(9, 0),
                end_time=datetime.time(10, 0), status=AppointmentStatus.PENDING,
                price_at_booking=self.service.price,
            )

    def test_completed_status_does_not_block_new_booking_over_it(self):
        # concluído libera o horário (BLOCKING_STATUSES não inclui completed)
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.COMPLETED,
        )
        appointment = create_appointment(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.service, date=self.monday, start_time=datetime.time(9, 0),
        )
        self.assertEqual(appointment.status, AppointmentStatus.PENDING)


class CancelAppointmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)

    def test_cancel_pending_appointment(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        cancel_appointment(appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CANCELED)

    def test_cancel_confirmed_appointment(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.CONFIRMED,
        )
        cancel_appointment(appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CANCELED)

    def test_cannot_cancel_already_completed_appointment(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.COMPLETED,
        )
        with self.assertRaises(ValidationError):
            cancel_appointment(appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)

    def test_cannot_cancel_already_canceled_appointment(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.CANCELED,
        )
        with self.assertRaises(ValidationError):
            cancel_appointment(appointment)

    def test_admin_cancel_does_not_mark_canceled_by_client(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        cancel_appointment(appointment)
        appointment.refresh_from_db()
        self.assertFalse(appointment.canceled_by_client)

    def test_admin_cancel_does_not_create_notification(self):
        from apps.notifications.models import TenantNotification

        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        cancel_appointment(appointment)
        self.assertFalse(TenantNotification.objects.filter(tenant=self.tenant).exists())

    def test_client_cancel_marks_canceled_by_client(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        cancel_appointment(appointment, canceled_by_client=True)
        appointment.refresh_from_db()
        self.assertTrue(appointment.canceled_by_client)
        self.assertEqual(appointment.status, AppointmentStatus.CANCELED)

    def test_client_cancel_creates_tenant_notification(self):
        from apps.notifications.models import TenantNotification, TenantNotificationKind

        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        cancel_appointment(appointment, canceled_by_client=True)
        notification = TenantNotification.objects.get(tenant=self.tenant)
        self.assertEqual(notification.kind, TenantNotificationKind.APPOINTMENT_CANCELED_BY_CLIENT)
        self.assertEqual(notification.appointment_id, appointment.pk)
        self.assertIn(self.client_.name, notification.message)
        self.assertFalse(notification.is_read)

    def test_client_cancel_notification_created_even_without_whatsapp_toggle(self):
        """Decisão do usuário em 2026-07-31: a notificação interna independe
        do toggle de redirecionamento por WhatsApp (esse controla só o
        cliente ser levado pro WhatsApp, não a notificação do salão)."""
        from apps.notifications.models import TenantNotification

        self.tenant.whatsapp_cancel_redirect_enabled = False
        self.tenant.save(update_fields=["whatsapp_cancel_redirect_enabled"])
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        cancel_appointment(appointment, canceled_by_client=True)
        self.assertTrue(TenantNotification.objects.filter(tenant=self.tenant).exists())


class UpcomingAppointmentsForClientTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )

    def test_only_future_pending_or_confirmed_shown(self):
        today = datetime.date.today()
        past = today - datetime.timedelta(days=5)
        future = today + datetime.timedelta(days=5)

        book(
            self.tenant, self.employee, self.service, self.client_,
            past, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.CONFIRMED,
        )
        completed_future = book(
            self.tenant, self.employee, self.service, self.client_,
            future, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.COMPLETED,
        )
        upcoming = book(
            self.tenant, self.employee, self.service, self.client_,
            future, datetime.time(11, 0), datetime.time(12, 0),
            status=AppointmentStatus.PENDING,
        )

        result = list(upcoming_appointments_for_client(self.client_))
        self.assertEqual(result, [upcoming])
        self.assertNotIn(completed_future, result)

    def test_isolated_per_client(self):
        other_client = make_client(self.tenant, phone="+5511777770000", name="Outro")
        future = datetime.date.today() + datetime.timedelta(days=5)
        book(
            self.tenant, self.employee, self.service, other_client,
            future, datetime.time(9, 0), datetime.time(10, 0),
        )
        self.assertEqual(list(upcoming_appointments_for_client(self.client_)), [])


class CompleteAppointmentTest(TestCase):
    """RF16 / regra 3 do CLAUDE.md — a operação central do sistema."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)

    def _make_appointment(self, status=AppointmentStatus.CONFIRMED):
        return book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0), status=status,
        )

    def test_completes_appointment_and_generates_commission_and_cash(self):
        appointment = self._make_appointment()
        commission = complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)

        self.assertEqual(commission.employee, self.employee)
        self.assertEqual(commission.base_amount, Decimal("100.00"))
        # Ana (funcionária padrão do helper make_employee) tem comissão de 40%
        self.assertEqual(commission.commission_type, "percentage")
        self.assertEqual(commission.calculated_amount, Decimal("40.00"))
        self.assertEqual(commission.status, CommissionStatus.PENDING)

        cash_txn = CashTransaction.objects.get(related_appointment=appointment)
        self.assertEqual(cash_txn.type, CashFlowType.IN)
        self.assertEqual(cash_txn.category, CashCategory.SERVICE_SALE)
        self.assertEqual(cash_txn.amount, Decimal("100.00"))

    def test_paying_with_client_credit_does_not_create_cash_transaction(self):
        """Decisão do usuário: pagar com o crédito do cliente não duplica
        receita no Caixa — o dinheiro já entrou na recarga."""
        from apps.clients.models import ClientCreditTransaction
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("200"), payment_method="pix", created_by=self.admin
        )
        appointment = self._make_appointment()
        commission = complete_appointment(
            appointment=appointment, payment_method="client_credit", created_by=self.admin,
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)
        # comissão continua normal, independe da forma de pagamento
        self.assertEqual(commission.calculated_amount, Decimal("40.00"))

        self.assertFalse(CashTransaction.objects.filter(related_appointment=appointment).exists())

        self.client_.refresh_from_db()
        self.assertEqual(self.client_.credit_balance, Decimal("100.00"))
        redemption = ClientCreditTransaction.objects.get(related_appointment=appointment)
        self.assertEqual(redemption.amount, Decimal("100.00"))

    def test_partial_credit_creates_reduced_cash_transaction_for_remainder(self):
        """Pedido do usuário: crédito insuficiente pro total pode ser abatido
        parcialmente — o resto é cobrado normalmente por outra forma."""
        from apps.clients.models import ClientCreditTransaction
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("30"), payment_method="pix", created_by=self.admin
        )
        appointment = self._make_appointment()  # serviço de R$100
        complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
            credit_amount=Decimal("30"),
        )
        cash_txn = CashTransaction.objects.get(related_appointment=appointment)
        self.assertEqual(cash_txn.amount, Decimal("70.00"))  # 100 - 30 de crédito
        self.assertEqual(cash_txn.payment_method, "cash")

        self.client_.refresh_from_db()
        self.assertEqual(self.client_.credit_balance, Decimal("0.00"))
        redemption = ClientCreditTransaction.objects.get(related_appointment=appointment)
        self.assertEqual(redemption.amount, Decimal("30.00"))

    def test_zero_credit_amount_behaves_like_no_credit(self):
        appointment = self._make_appointment()
        complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
            credit_amount=Decimal("0"),
        )
        cash_txn = CashTransaction.objects.get(related_appointment=appointment)
        self.assertEqual(cash_txn.amount, Decimal("100.00"))

    def test_credit_amount_equal_to_total_creates_no_cash_transaction(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("100"), payment_method="pix", created_by=self.admin
        )
        appointment = self._make_appointment()
        complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
            credit_amount=Decimal("100"),
        )
        self.assertFalse(CashTransaction.objects.filter(related_appointment=appointment).exists())
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.credit_balance, Decimal("0.00"))

    def test_credit_amount_greater_than_total_rejected(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("500"), payment_method="pix", created_by=self.admin
        )
        appointment = self._make_appointment()  # total = R$100
        with self.assertRaises(ValidationError):
            complete_appointment(
                appointment=appointment, payment_method="cash", created_by=self.admin,
                credit_amount=Decimal("150"),
            )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)  # nada foi concluído

    def test_credit_amount_greater_than_balance_rejected_by_ledger_check(self):
        appointment = self._make_appointment()
        with self.assertRaises(ValidationError):
            complete_appointment(
                appointment=appointment, payment_method="cash", created_by=self.admin,
                credit_amount=Decimal("10"),  # cliente não tem nenhum crédito
            )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_partial_credit_applied_to_service_before_products(self):
        from apps.clients.services import add_client_credit
        from apps.inventory.services import create_product, register_stock_movement

        add_client_credit(
            self.client_, amount=Decimal("120"), payment_method="pix", created_by=self.admin
        )
        product = create_product(
            tenant=self.tenant, name="Ampola", unit="un",
            cost_price=Decimal("5"), sale_price=Decimal("50.00"), min_stock_alert=Decimal("1"),
        )
        register_stock_movement(
            tenant=self.tenant, product=product, movement_type="in",
            quantity=Decimal("10"), unit_price=Decimal("1"), reason="purchase",
            created_by=self.admin,
        )
        appointment = self._make_appointment()  # serviço R$100 + produto R$50 = 150
        complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
            product_usage=[{"product": product, "quantity": Decimal("1"), "unit_price": Decimal("50.00")}],
            credit_amount=Decimal("120"),
        )
        # 120 de crédito cobre o serviço inteiro (100) + 20 do produto — resta 30 em dinheiro
        self.assertFalse(
            CashTransaction.objects.filter(
                related_appointment=appointment, category=CashCategory.SERVICE_SALE
            ).exists()
        )
        product_txn = CashTransaction.objects.get(
            related_appointment=appointment, category=CashCategory.PRODUCT_SALE
        )
        self.assertEqual(product_txn.amount, Decimal("30.00"))

    def test_commission_uses_employee_service_override(self):
        from apps.employees.services import link_service

        link_service(
            self.employee, self.service, commission_type="fixed", commission_value=Decimal("25.00")
        )
        appointment = self._make_appointment()
        commission = complete_appointment(
            appointment=appointment, payment_method="pix", created_by=self.admin,
        )
        self.assertEqual(commission.commission_type, "fixed")
        self.assertEqual(commission.calculated_amount, Decimal("25.00"))

    def test_price_snapshot_used_even_if_service_price_changed_later(self):
        appointment = self._make_appointment()
        from apps.services.services import update_service

        update_service(
            self.service, name=self.service.name, duration_minutes=60, price=Decimal("500.00")
        )
        commission = complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
        )
        self.assertEqual(commission.base_amount, Decimal("100.00"))

    def test_completing_with_product_usage_registers_stock_and_cash(self):
        from apps.inventory.services import register_stock_movement

        product = create_product(
            tenant=self.tenant, name="Ampola", unit="un",
            cost_price=Decimal("5.00"), sale_price=Decimal("15.00"), min_stock_alert=Decimal("2"),
        )
        register_stock_movement(
            tenant=self.tenant, product=product, movement_type="in",
            quantity=Decimal("10"), unit_price=Decimal("5.00"), reason="purchase",
            created_by=self.admin,
        )
        appointment = self._make_appointment()
        complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
            product_usage=[{"product": product, "quantity": Decimal("1"), "unit_price": Decimal("15.00")}],
        )
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("9"))

        product_cash_txn = CashTransaction.objects.get(category=CashCategory.PRODUCT_SALE)
        self.assertEqual(product_cash_txn.amount, Decimal("15.00"))
        self.assertEqual(product_cash_txn.related_appointment, appointment)

    def test_recipe_item_consumed_automatically_without_charging_client(self):
        """RF48 — insumo cadastrado na receita do serviço é abatido sozinho,
        sem gerar CashTransaction nem entrar no total cobrado do cliente
        (diferente de product_usage, a venda casada manual)."""
        insumo = create_product(
            tenant=self.tenant, name="Tintura Loiro", unit="ml",
            cost_price=Decimal("0.50"), sale_price=Decimal("0.00"), min_stock_alert=Decimal("10"),
            is_for_sale=False,
        )
        from apps.inventory.services import register_stock_movement

        register_stock_movement(
            tenant=self.tenant, product=insumo, movement_type="in",
            quantity=Decimal("100"), unit_price=Decimal("0.50"), reason="purchase",
            created_by=self.admin,
        )
        add_recipe_item(service=self.service, product=insumo, quantity=Decimal("30"))
        appointment = self._make_appointment()

        commission = complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
        )

        insumo.refresh_from_db()
        self.assertEqual(insumo.current_stock, Decimal("70"))
        movement = StockMovement.objects.get(product=insumo, reason=MovementReason.RECIPE_USE)
        self.assertEqual(movement.quantity, Decimal("30"))
        self.assertEqual(movement.related_appointment, appointment)
        self.assertFalse(
            CashTransaction.objects.filter(category=CashCategory.PRODUCT_SALE).exists()
        )
        # comissão e venda do serviço continuam intactas, sem influência do insumo
        service_cash_txn = CashTransaction.objects.get(category=CashCategory.SERVICE_SALE)
        self.assertEqual(service_cash_txn.amount, Decimal("100.00"))
        self.assertEqual(commission.calculated_amount, Decimal("40.00"))

    def test_recipe_insufficient_stock_does_not_block_and_returns_warning(self):
        """Decisão do usuário: falta de insumo NÃO trava a conclusão — o
        estoque fica negativo e um aviso é anexado em stock_warnings."""
        insumo = create_product(
            tenant=self.tenant, name="Tintura Loiro", unit="ml",
            cost_price=Decimal("0.50"), sale_price=Decimal("0.00"), min_stock_alert=Decimal("10"),
            is_for_sale=False,
        )
        add_recipe_item(service=self.service, product=insumo, quantity=Decimal("30"))
        appointment = self._make_appointment()  # insumo continua com estoque 0

        warnings = []
        complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
            stock_warnings=warnings,
        )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)
        insumo.refresh_from_db()
        self.assertEqual(insumo.current_stock, Decimal("-30"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("Tintura Loiro", warnings[0])

    def test_recipe_consumed_even_when_service_covered_by_package(self):
        """O insumo é gasto de verdade mesmo quando o serviço em si não é
        cobrado de novo por já estar coberto por um pacote de mensalidade."""
        from apps.clients.services import assign_package_to_client, create_package

        insumo = create_product(
            tenant=self.tenant, name="Tintura Loiro", unit="ml",
            cost_price=Decimal("0.50"), sale_price=Decimal("0.00"), min_stock_alert=Decimal("10"),
            is_for_sale=False,
        )
        from apps.inventory.services import register_stock_movement

        register_stock_movement(
            tenant=self.tenant, product=insumo, movement_type="in",
            quantity=Decimal("100"), unit_price=Decimal("0.50"), reason="purchase",
            created_by=self.admin,
        )
        add_recipe_item(service=self.service, product=insumo, quantity=Decimal("30"))
        package = create_package(
            tenant=self.tenant, name="Corte Ilimitado", price=Decimal("150.00"),
            service_ids=[self.service.pk], generates_commission=True, created_by=self.admin,
        )
        assign_package_to_client(
            self.client_, package=package, payment_method="pix", created_by=self.admin,
        )
        appointment = self._make_appointment()
        appointment.package = package
        appointment.save(update_fields=["package"])

        complete_appointment(appointment=appointment, payment_method="cash", created_by=self.admin)

        insumo.refresh_from_db()
        self.assertEqual(insumo.current_stock, Decimal("70"))
        self.assertFalse(
            CashTransaction.objects.filter(
                related_appointment=appointment, category=CashCategory.SERVICE_SALE
            ).exists()
        )

    def test_recipe_consumption_rolls_back_on_atomicity_failure(self):
        """Mesma garantia do teste de atomicidade já existente, agora
        cobrindo o novo loop de receita: se algo mais adiante falhar (aqui,
        crédito maior que o total), o StockMovement do insumo TAMBÉM não
        pode sobrar persistido."""
        from apps.clients.services import add_client_credit

        insumo = create_product(
            tenant=self.tenant, name="Tintura Loiro", unit="ml",
            cost_price=Decimal("0.50"), sale_price=Decimal("0.00"), min_stock_alert=Decimal("10"),
            is_for_sale=False,
        )
        from apps.inventory.services import register_stock_movement

        register_stock_movement(
            tenant=self.tenant, product=insumo, movement_type="in",
            quantity=Decimal("100"), unit_price=Decimal("0.50"), reason="purchase",
            created_by=self.admin,
        )
        add_recipe_item(service=self.service, product=insumo, quantity=Decimal("30"))
        add_client_credit(
            self.client_, amount=Decimal("500"), payment_method="pix", created_by=self.admin
        )
        appointment = self._make_appointment()  # total = R$100

        with self.assertRaises(ValidationError):
            complete_appointment(
                appointment=appointment, payment_method="cash", created_by=self.admin,
                credit_amount=Decimal("150"),  # maior que o total — rejeitado depois do loop de receita
            )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)
        insumo.refresh_from_db()
        self.assertEqual(insumo.current_stock, Decimal("100"))  # nada consumido
        self.assertFalse(StockMovement.objects.filter(reason=MovementReason.RECIPE_USE).exists())

    def test_cannot_complete_already_completed_appointment(self):
        appointment = self._make_appointment(status=AppointmentStatus.COMPLETED)
        with self.assertRaises(ValidationError):
            complete_appointment(appointment=appointment, payment_method="cash", created_by=self.admin)

    def test_cannot_complete_canceled_appointment(self):
        appointment = self._make_appointment(status=AppointmentStatus.CANCELED)
        with self.assertRaises(ValidationError):
            complete_appointment(appointment=appointment, payment_method="cash", created_by=self.admin)

    def test_atomicity_rollback_on_forced_failure(self):
        """O teste mais importante desta etapa: se o abatimento de estoque
        falhar (produto sem saldo suficiente), NADA do resto pode ter sido
        persistido — nem status, nem Commission, nem CashTransaction."""
        product = create_product(
            tenant=self.tenant, name="Sem Estoque", unit="un",
            cost_price=Decimal("5.00"), sale_price=Decimal("15.00"), min_stock_alert=Decimal("1"),
        )
        appointment = self._make_appointment()

        commissions_before = Commission.objects.count()
        cash_before = CashTransaction.objects.count()

        with self.assertRaises(ValidationError):
            complete_appointment(
                appointment=appointment, payment_method="cash", created_by=self.admin,
                product_usage=[
                    {"product": product, "quantity": Decimal("999"), "unit_price": Decimal("15.00")}
                ],
            )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)  # não virou completed
        self.assertEqual(Commission.objects.count(), commissions_before)
        self.assertEqual(CashTransaction.objects.count(), cash_before)

    def test_debt_amount_creates_reduced_cash_and_debt_transaction(self):
        """Decisão do usuário em 2026-08-06: comanda com pagamento parcial —
        a diferença vira débito do cliente, cobrado numa comanda futura."""
        from apps.clients.models import ClientDebtTransaction

        appointment = self._make_appointment()  # serviço R$100
        complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
            debt_amount=Decimal("30"),
        )
        cash_txn = CashTransaction.objects.get(related_appointment=appointment)
        self.assertEqual(cash_txn.amount, Decimal("70.00"))  # 100 - 30 fiado

        self.client_.refresh_from_db()
        self.assertEqual(self.client_.debt_balance, Decimal("30.00"))
        debt_entry = ClientDebtTransaction.objects.get(related_appointment=appointment)
        self.assertEqual(debt_entry.type, "in")
        self.assertEqual(debt_entry.amount, Decimal("30.00"))

    def test_debt_amount_does_not_affect_commission(self):
        """A decisão de negócio mais importante desta etapa: o funcionário
        recebe a comissão cheia mesmo se parte da comanda ficou como fiado —
        o salão assume o risco do calote, não o funcionário."""
        appointment = self._make_appointment()  # serviço R$100, comissão 40%
        commission = complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
            debt_amount=Decimal("100"),  # comanda inteira ficou como fiado
        )
        self.assertEqual(commission.base_amount, Decimal("100.00"))
        self.assertEqual(commission.calculated_amount, Decimal("40.00"))

    def test_credit_plus_debt_greater_than_total_rejected(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("50"), payment_method="pix", created_by=self.admin
        )
        appointment = self._make_appointment()  # total = R$100
        with self.assertRaises(ValidationError):
            complete_appointment(
                appointment=appointment, payment_method="cash", created_by=self.admin,
                credit_amount=Decimal("50"), debt_amount=Decimal("60"),
            )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_collect_prior_debt_amount_reduces_balance_and_creates_cash_transaction(self):
        from apps.clients.models import ClientDebtTransaction
        from apps.clients.services import record_client_debt

        record_client_debt(
            self.client_, amount=Decimal("40"), appointment=None, created_by=self.admin
        )
        appointment = self._make_appointment()  # serviço R$100
        complete_appointment(
            appointment=appointment, payment_method="pix", created_by=self.admin,
            collect_prior_debt_amount=Decimal("40"),
        )
        # cobrança do serviço (100) + cobrança do débito anterior (40) = duas CashTransaction
        service_txn = CashTransaction.objects.get(
            related_appointment=appointment, category=CashCategory.SERVICE_SALE
        )
        self.assertEqual(service_txn.amount, Decimal("100.00"))
        debt_payment_txn = CashTransaction.objects.get(category=CashCategory.CLIENT_DEBT_PAYMENT)
        self.assertEqual(debt_payment_txn.amount, Decimal("40.00"))

        self.client_.refresh_from_db()
        self.assertEqual(self.client_.debt_balance, Decimal("0.00"))
        settle_entry = ClientDebtTransaction.objects.get(
            related_cash_transaction=debt_payment_txn
        )
        self.assertEqual(settle_entry.type, "out")

    def test_collect_prior_debt_amount_greater_than_balance_rejected(self):
        from apps.clients.services import record_client_debt

        record_client_debt(
            self.client_, amount=Decimal("10"), appointment=None, created_by=self.admin
        )
        appointment = self._make_appointment()
        with self.assertRaises(ValidationError):
            complete_appointment(
                appointment=appointment, payment_method="pix", created_by=self.admin,
                collect_prior_debt_amount=Decimal("50"),
            )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)


class StartWalkInServiceTest(TestCase):
    """Serviço extra vendido na hora, cliente já no salão — sem passar pela
    checagem de disponibilidade futura (`is_slot_available`)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Manicure", duration_minutes=30, price=Decimal("45.00")
        )

    def test_creates_in_progress_appointment_for_today(self):
        appointment = start_walk_in_service(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.service, created_by=self.admin,
        )
        self.assertEqual(appointment.status, AppointmentStatus.IN_PROGRESS)
        self.assertEqual(appointment.date, datetime.date.today())
        self.assertEqual(appointment.price_at_booking, Decimal("45.00"))

    def test_rejects_inactive_employee(self):
        self.employee.is_active = False
        self.employee.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            start_walk_in_service(
                tenant=self.tenant, client=self.client_, employee=self.employee,
                service=self.service, created_by=self.admin,
            )

    def test_rejects_inactive_service(self):
        self.service.is_active = False
        self.service.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            start_walk_in_service(
                tenant=self.tenant, client=self.client_, employee=self.employee,
                service=self.service, created_by=self.admin,
            )

    def test_rejects_cross_tenant_employee(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        other_employee = make_employee(other_tenant, email="other@salao-b.com")
        with self.assertRaises(ValidationError):
            start_walk_in_service(
                tenant=self.tenant, client=self.client_, employee=other_employee,
                service=self.service, created_by=self.admin,
            )


class RemoveAppointmentFromComandaTest(TestCase):
    """Corrigir um serviço adicionado por engano na comanda — pedido do
    usuário: "se eu errar, não consigo mais remover". Volta pra cancelado,
    liberando o horário do profissional."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )

    def _in_progress_appointment(self):
        return Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=datetime.date.today(), start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )

    def test_removes_appointment_by_marking_canceled(self):
        appointment = self._in_progress_appointment()
        remove_appointment_from_comanda(appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CANCELED)

    def test_frees_up_the_employee_slot(self):
        appointment = self._in_progress_appointment()
        remove_appointment_from_comanda(appointment)
        # o mesmo profissional/horário pode ser usado de novo (não bate na
        # unique_active_appointment_per_slot, já que o status não é mais
        # "ativo")
        new_appointment = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=appointment.date, start_time=appointment.start_time, end_time=appointment.end_time,
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )
        self.assertIsNotNone(new_appointment.pk)

    def test_rejects_appointment_not_in_progress(self):
        appointment = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=datetime.date.today(), start_time=datetime.time(11, 0), end_time=datetime.time(12, 0),
            status=AppointmentStatus.CONFIRMED, price_at_booking=Decimal("100.00"),
        )
        with self.assertRaises(ValidationError):
            remove_appointment_from_comanda(appointment)

    def test_rejects_already_completed_appointment(self):
        appointment = self._in_progress_appointment()
        complete_appointment(appointment=appointment, payment_method="cash", created_by=self.admin)
        with self.assertRaises(ValidationError):
            remove_appointment_from_comanda(appointment)


class CompleteClientComandaTest(TestCase):
    """Fechamento de comanda com MAIS DE UM serviço pro mesmo cliente na
    mesma visita (ex.: corte + manicure, profissionais diferentes) — um
    pagamento só pra tudo, mas comissão e caixa continuam por atendimento."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.ana = make_employee(cls.tenant, email="ana@salao-a.com", full_name="Ana Silva")
        cls.julia = make_employee(cls.tenant, email="julia@salao-a.com", full_name="Júlia Mendes")
        cls.client_ = make_client(cls.tenant)
        cls.corte = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.manicure = create_service(
            tenant=cls.tenant, name="Manicure", duration_minutes=30, price=Decimal("45.00")
        )

    def _in_progress(self, employee, service, start_time=datetime.time(9, 0)):
        return Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=employee, service=service,
            date=datetime.date.today(), start_time=start_time,
            end_time=datetime.time(start_time.hour + 1, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=service.price,
        )

    def test_completes_both_appointments_with_one_payment(self):
        corte_appt = self._in_progress(self.ana, self.corte)
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))
        complete_client_comanda(
            appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
        )
        corte_appt.refresh_from_db()
        manicure_appt.refresh_from_db()
        self.assertEqual(corte_appt.status, AppointmentStatus.COMPLETED)
        self.assertEqual(manicure_appt.status, AppointmentStatus.COMPLETED)

    def test_each_appointment_generates_its_own_commission(self):
        corte_appt = self._in_progress(self.ana, self.corte)
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))
        complete_client_comanda(
            appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
        )
        corte_commission = Commission.objects.get(appointment=corte_appt)
        manicure_commission = Commission.objects.get(appointment=manicure_appt)
        self.assertEqual(corte_commission.employee, self.ana)
        self.assertEqual(manicure_commission.employee, self.julia)
        self.assertEqual(corte_commission.calculated_amount, Decimal("40.00"))  # 40% de 100
        self.assertEqual(manicure_commission.calculated_amount, Decimal("18.00"))  # 40% de 45

    def test_each_appointment_generates_its_own_cash_transaction(self):
        corte_appt = self._in_progress(self.ana, self.corte)
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))
        complete_client_comanda(
            appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
        )
        self.assertTrue(
            CashTransaction.objects.filter(
                related_appointment=corte_appt, amount=Decimal("100.00")
            ).exists()
        )
        self.assertTrue(
            CashTransaction.objects.filter(
                related_appointment=manicure_appt, amount=Decimal("45.00")
            ).exists()
        )

    def test_product_usage_attributed_to_correct_appointment(self):
        product = create_product(
            tenant=self.tenant, name="Esmalte", unit="un",
            cost_price=Decimal("5"), sale_price=Decimal("20"), min_stock_alert=Decimal("1"),
        )
        from apps.inventory.services import register_stock_movement

        register_stock_movement(
            tenant=self.tenant, product=product, movement_type="in",
            quantity=Decimal("10"), unit_price=Decimal("1"), reason="purchase",
            created_by=self.admin,
        )
        corte_appt = self._in_progress(self.ana, self.corte)
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))
        complete_client_comanda(
            appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
            product_usage_by_appointment={
                manicure_appt.pk: [
                    {"product": product, "quantity": Decimal("1"), "unit_price": Decimal("20")}
                ],
            },
        )
        self.assertTrue(
            CashTransaction.objects.filter(
                related_appointment=manicure_appt, category=CashCategory.PRODUCT_SALE
            ).exists()
        )
        self.assertFalse(
            CashTransaction.objects.filter(
                related_appointment=corte_appt, category=CashCategory.PRODUCT_SALE
            ).exists()
        )

    def test_rejects_appointments_from_different_clients(self):
        other_client = make_client(self.tenant, phone="+5511988880000", name="Outra Cliente")
        corte_appt = self._in_progress(self.ana, self.corte)
        other_appt = Appointment.objects.create(
            tenant=self.tenant, client=other_client, employee=self.julia, service=self.manicure,
            date=datetime.date.today(), start_time=datetime.time(10, 0), end_time=datetime.time(10, 30),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=self.manicure.price,
        )
        with self.assertRaises(ValidationError):
            complete_client_comanda(
                appointments=[corte_appt, other_appt], payment_method="cash", created_by=self.admin,
            )

    def test_empty_list_rejected(self):
        with self.assertRaises(ValidationError):
            complete_client_comanda(appointments=[], payment_method="cash", created_by=self.admin)

    def test_insufficient_combined_credit_rolls_back_everything(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("120"), payment_method="pix", created_by=self.admin
        )
        corte_appt = self._in_progress(self.ana, self.corte)  # 100
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))  # 45 -> soma 145 > 120
        with self.assertRaises(ValidationError):
            complete_client_comanda(
                appointments=[corte_appt, manicure_appt], payment_method="client_credit",
                created_by=self.admin,
            )
        corte_appt.refresh_from_db()
        manicure_appt.refresh_from_db()
        self.assertEqual(corte_appt.status, AppointmentStatus.IN_PROGRESS)
        self.assertEqual(manicure_appt.status, AppointmentStatus.IN_PROGRESS)
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.credit_balance, Decimal("120.00"))

    def test_sufficient_combined_credit_succeeds_without_cash_transaction(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("200"), payment_method="pix", created_by=self.admin
        )
        corte_appt = self._in_progress(self.ana, self.corte)
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))
        complete_client_comanda(
            appointments=[corte_appt, manicure_appt], payment_method="client_credit",
            created_by=self.admin,
        )
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.credit_balance, Decimal("55.00"))  # 200 - 100 - 45
        self.assertFalse(
            CashTransaction.objects.filter(
                related_appointment__in=[corte_appt, manicure_appt]
            ).exists()
        )

    def test_explicit_group_credit_amount_allocated_in_order(self):
        """Pedido do usuário: crédito insuficiente pro total do GRUPO pode
        ser abatido parcialmente — o admin digita um valor só, alocado em
        ordem (primeiro atendimento primeiro)."""
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("120"), payment_method="pix", created_by=self.admin
        )
        corte_appt = self._in_progress(self.ana, self.corte)  # 100
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))  # 45
        complete_client_comanda(
            appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
            credit_amount=Decimal("120"),
        )
        # corte (100) totalmente coberto por crédito — sem CashTransaction
        self.assertFalse(CashTransaction.objects.filter(related_appointment=corte_appt).exists())
        # manicure (45): sobrou 20 de crédito (120-100), resto (25) em dinheiro
        manicure_txn = CashTransaction.objects.get(related_appointment=manicure_appt)
        self.assertEqual(manicure_txn.amount, Decimal("25.00"))
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.credit_balance, Decimal("0.00"))

    def test_explicit_group_credit_amount_over_total_rejected(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("500"), payment_method="pix", created_by=self.admin
        )
        corte_appt = self._in_progress(self.ana, self.corte)  # 100
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))  # 45 -> total 145
        with self.assertRaises(ValidationError):
            complete_client_comanda(
                appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
                credit_amount=Decimal("200"),
            )
        corte_appt.refresh_from_db()
        manicure_appt.refresh_from_db()
        self.assertEqual(corte_appt.status, AppointmentStatus.IN_PROGRESS)
        self.assertEqual(manicure_appt.status, AppointmentStatus.IN_PROGRESS)

    def test_explicit_group_debt_amount_allocated_in_order(self):
        """Mesma alocação em ordem do crédito de grupo, mas pro valor que
        fica em aberto como fiado."""
        corte_appt = self._in_progress(self.ana, self.corte)  # 100
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))  # 45
        complete_client_comanda(
            appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
            credit_amount=Decimal("0"), debt_amount=Decimal("120"),
        )
        # corte (100) totalmente coberto por fiado — sem CashTransaction
        self.assertFalse(CashTransaction.objects.filter(related_appointment=corte_appt).exists())
        # manicure (45): sobrou 20 de fiado (120-100), resto (25) em dinheiro
        manicure_txn = CashTransaction.objects.get(related_appointment=manicure_appt)
        self.assertEqual(manicure_txn.amount, Decimal("25.00"))
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.debt_balance, Decimal("120.00"))

    def test_explicit_group_debt_amount_over_total_rejected(self):
        corte_appt = self._in_progress(self.ana, self.corte)  # 100
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))  # 45 -> total 145
        with self.assertRaises(ValidationError):
            complete_client_comanda(
                appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
                credit_amount=Decimal("0"), debt_amount=Decimal("200"),
            )
        corte_appt.refresh_from_db()
        manicure_appt.refresh_from_db()
        self.assertEqual(corte_appt.status, AppointmentStatus.IN_PROGRESS)
        self.assertEqual(manicure_appt.status, AppointmentStatus.IN_PROGRESS)

    def test_collect_prior_debt_amount_attributed_to_first_appointment_only(self):
        from apps.clients.services import record_client_debt

        record_client_debt(
            self.client_, amount=Decimal("40"), appointment=None, created_by=self.admin
        )
        corte_appt = self._in_progress(self.ana, self.corte)  # 100
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))  # 45
        complete_client_comanda(
            appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
            credit_amount=Decimal("0"), collect_prior_debt_amount=Decimal("40"),
        )
        debt_payment_txn = CashTransaction.objects.get(category=CashCategory.CLIENT_DEBT_PAYMENT)
        self.assertEqual(debt_payment_txn.related_appointment, corte_appt)
        self.assertEqual(debt_payment_txn.amount, Decimal("40.00"))
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.debt_balance, Decimal("0.00"))

    def test_group_rolls_back_atomically_on_debt_over_total(self):
        """Se o grupo falhar por causa do débito, nada foi persistido —
        mesma garantia atômica que o crédito de grupo já tem."""
        corte_appt = self._in_progress(self.ana, self.corte)
        manicure_appt = self._in_progress(self.julia, self.manicure, datetime.time(10, 0))
        commissions_before = Commission.objects.count()
        with self.assertRaises(ValidationError):
            complete_client_comanda(
                appointments=[corte_appt, manicure_appt], payment_method="cash", created_by=self.admin,
                credit_amount=Decimal("0"), debt_amount=Decimal("9999"),
            )
        self.assertEqual(Commission.objects.count(), commissions_before)
        corte_appt.refresh_from_db()
        manicure_appt.refresh_from_db()
        self.assertEqual(corte_appt.status, AppointmentStatus.IN_PROGRESS)
        self.assertEqual(manicure_appt.status, AppointmentStatus.IN_PROGRESS)


class BuildProductUsageTest(TestCase):
    """RF16: fechamento de comanda permite múltiplos produtos, com o preço
    unitário sempre vindo do cadastro (`Product.sale_price`), nunca digitado."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.shampoo = create_product(
            tenant=cls.tenant, name="Shampoo", unit="un",
            cost_price=Decimal("10.00"), sale_price=Decimal("30.00"), min_stock_alert=Decimal("1"),
        )
        cls.ampola = create_product(
            tenant=cls.tenant, name="Ampola", unit="un",
            cost_price=Decimal("5.00"), sale_price=Decimal("15.00"), min_stock_alert=Decimal("1"),
        )

    def test_builds_usage_with_product_sale_price(self):
        usage = build_product_usage(
            tenant=self.tenant,
            product_ids=[str(self.shampoo.pk), str(self.ampola.pk)],
            quantities=["2", "1"],
        )
        self.assertEqual(len(usage), 2)
        self.assertEqual(usage[0], {"product": self.shampoo, "quantity": Decimal("2"), "unit_price": Decimal("30.00")})
        self.assertEqual(usage[1], {"product": self.ampola, "quantity": Decimal("1"), "unit_price": Decimal("15.00")})

    def test_accepts_comma_decimal_quantity_for_measured_unit(self):
        oleo = create_product(
            tenant=self.tenant, name="Óleo a Granel", unit="ml",
            cost_price=Decimal("0.10"), sale_price=Decimal("0.50"), min_stock_alert=Decimal("1"),
        )
        usage = build_product_usage(
            tenant=self.tenant, product_ids=[str(oleo.pk)], quantities=["1,5"],
        )
        self.assertEqual(usage[0]["quantity"], Decimal("1.5"))

    def test_fractional_quantity_rejected_for_whole_unit_product(self):
        """Regra nova: "un"/"par"/"cx" não aceitam quantidade fracionada —
        não dá pra vender "2,04" unidades de um produto."""
        with self.assertRaises(ValidationError):
            build_product_usage(
                tenant=self.tenant, product_ids=[str(self.shampoo.pk)], quantities=["2,04"],
            )

    def test_ignores_blank_rows(self):
        usage = build_product_usage(
            tenant=self.tenant,
            product_ids=[str(self.shampoo.pk), ""],
            quantities=["2", ""],
        )
        self.assertEqual(len(usage), 1)

    def test_empty_lists_return_empty_usage(self):
        self.assertEqual(build_product_usage(tenant=self.tenant, product_ids=[], quantities=[]), [])

    def test_duplicate_product_rejected(self):
        with self.assertRaises(ValidationError):
            build_product_usage(
                tenant=self.tenant,
                product_ids=[str(self.shampoo.pk), str(self.shampoo.pk)],
                quantities=["1", "1"],
            )

    def test_zero_quantity_rejected(self):
        with self.assertRaises(ValidationError):
            build_product_usage(
                tenant=self.tenant, product_ids=[str(self.shampoo.pk)], quantities=["0"],
            )

    def test_inactive_product_rejected(self):
        from apps.inventory.services import set_product_active

        set_product_active(self.shampoo, False)
        with self.assertRaises(ValidationError):
            build_product_usage(
                tenant=self.tenant, product_ids=[str(self.shampoo.pk)], quantities=["1"],
            )

    def test_product_from_other_tenant_rejected(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        other_product = create_product(
            tenant=other_tenant, name="Outro", unit="un",
            cost_price=Decimal("1.00"), sale_price=Decimal("2.00"), min_stock_alert=Decimal("1"),
        )
        with self.assertRaises(ValidationError):
            build_product_usage(
                tenant=self.tenant, product_ids=[str(other_product.pk)], quantities=["1"],
            )


class ConfirmAndNoShowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)

    def test_confirm_pending_appointment(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        confirm_appointment(appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_cannot_confirm_already_confirmed(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.CONFIRMED,
        )
        with self.assertRaises(ValidationError):
            confirm_appointment(appointment)

    def test_mark_no_show_from_pending(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        mark_no_show(appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.NO_SHOW)

    def test_cannot_mark_no_show_for_completed(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.COMPLETED,
        )
        with self.assertRaises(ValidationError):
            mark_no_show(appointment)


class AppointmentNewViewTest(TestCase):
    """Encaixe manual pelo painel (`scheduling:new`) — regressão do bug
    relatado pelo usuário: com `require_birthday_on_booking` ligado, o modal
    mostrava o erro "Aniversário é obrigatório" sem ter os campos pra
    preencher (o form não tinha birth_day/birth_month). Agora tem."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-novo-agendamento")
        cls.employee = make_employee(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        # precisa ser uma data futura de verdade (o form rejeita data passada)
        # e cair num dia com jornada configurada (`is_slot_available`).
        cls.monday = next_weekday(datetime.date.today() + datetime.timedelta(days=1), 0)
        set_working_hours(
            cls.employee,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}],
        )

    def _post_data(self, **overrides):
        data = {
            "service": self.service.pk,
            "employee": self.employee.pk,
            "date": self.monday.isoformat(),
            "time": "09:00",
            "phone": "11912345678",
            "name": "Cliente Novo",
            "birth_day": "",
            "birth_month": "",
            "notes": "",
        }
        data.update(overrides)
        return data

    def test_new_client_without_birthday_succeeds_when_not_required(self):
        self.client.force_login(self.admin)
        response = self.client.post("/painel/agenda/novo/", self._post_data())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Client.objects.filter(tenant=self.tenant, phone="11912345678").exists())

    def test_new_client_without_birthday_rejected_with_field_error_when_required(self):
        """O bug relatado: antes disso, esse erro aparecia sem nenhum campo
        pra corrigir. Agora `birth_day` é um campo de verdade do form."""
        self.tenant.require_birthday_on_booking = True
        self.tenant.save(update_fields=["require_birthday_on_booking"])
        self.client.force_login(self.admin)
        response = self.client.post("/painel/agenda/novo/", self._post_data())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Client.objects.filter(tenant=self.tenant, phone="11912345678").exists())
        body = response.content.decode()
        self.assertIn("Aniversário é obrigatório", body)
        self.assertIn('name="birth_day"', body)
        self.assertIn('name="birth_month"', body)

    def test_new_client_with_birthday_succeeds_when_required(self):
        self.tenant.require_birthday_on_booking = True
        self.tenant.save(update_fields=["require_birthday_on_booking"])
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/agenda/novo/", self._post_data(birth_day="15", birth_month="6")
        )
        self.assertEqual(response.status_code, 200)
        client_ = Client.objects.get(tenant=self.tenant, phone="11912345678")
        self.assertEqual(client_.birth_day, 15)
        self.assertEqual(client_.birth_month, 6)

    def test_existing_client_phone_not_blocked_even_when_required(self):
        """Cliente já cadastrado não é bloqueado retroativamente — o nome/
        aniversário digitados agora são ignorados, igual já acontecia com o
        nome antes dessa correção."""
        Client.objects.create(tenant=self.tenant, phone="11912345678", name="Maria Original")
        self.tenant.require_birthday_on_booking = True
        self.tenant.save(update_fields=["require_birthday_on_booking"])
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/agenda/novo/", self._post_data(name="Outro Nome", birth_day="", birth_month="")
        )
        self.assertEqual(response.status_code, 200)
        appointment = Appointment.objects.get(
            client__phone="11912345678", date=self.monday, start_time=datetime.time(9, 0)
        )
        self.assertEqual(appointment.client.name, "Maria Original")


class NewAppointmentClientLookupTest(TestCase):
    """Aviso HTMX no modal de encaixe manual quando o telefone digitado já é
    de um cliente cadastrado — `get_or_create_client` ignora o nome digitado
    nesse caso, então o atendente precisa saber disso antes de agendar."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-lookup")

    def test_shows_existing_client_name(self):
        Client.objects.create(tenant=self.tenant, phone="11912345678", name="Giovanna Ferreira")
        self.client.force_login(self.admin)
        response = self.client.get(
            "/painel/agenda/novo/cliente/", {"phone": "(11) 91234-5678"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Giovanna Ferreira")
        self.assertContains(response, "já cadastrado")

    def test_no_hint_for_unknown_phone(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            "/painel/agenda/novo/cliente/", {"phone": "11999998888"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "já cadastrado")

    def test_no_hint_for_incomplete_phone(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/agenda/novo/cliente/", {"phone": "119"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "já cadastrado")

    def test_does_not_leak_client_from_other_tenant(self):
        other_tenant, _ = make_tenant_with_admin("salao-lookup-outro")
        Client.objects.create(tenant=other_tenant, phone="11912345678", name="Giovanna Ferreira")
        self.client.force_login(self.admin)
        response = self.client.get(
            "/painel/agenda/novo/cliente/", {"phone": "11912345678"}
        )
        self.assertNotContains(response, "Giovanna Ferreira")

    def test_login_required(self):
        response = self.client.get(
            "/painel/agenda/novo/cliente/", {"phone": "11912345678"}
        )
        self.assertEqual(response.status_code, 302)


class AppointmentConfirmModalTest(TestCase):
    """"Confirmar" abre um modal com mensagem de WhatsApp pronta (editável)
    em vez de confirmar na hora — o POST de verdade só acontece quando o
    admin clica o botão dentro do modal (ver
    `apps.scheduling.views.appointment_confirm_prepare`)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-confirmmodal")
        cls.employee = make_employee(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        cls.monday = next_weekday(datetime.date(2026, 8, 1), 0)

    def test_login_required(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11988887777", name="Maria")
        appointment = book(
            self.tenant, self.employee, self.service, client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        response = self.client.get(f"/painel/agenda/{appointment.pk}/confirmar/preparar/")
        self.assertEqual(response.status_code, 302)

    def test_shows_editable_message_with_valid_phone(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11988887777", name="Maria")
        appointment = book(
            self.tenant, self.employee, self.service, client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/{appointment.pk}/confirmar/preparar/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("wa.me/5511988887777", body)
        self.assertIn("Maria", body)
        self.assertIn("Confirmar no WhatsApp", body)
        # a confirmação de verdade ainda não aconteceu, só abrir o modal
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.PENDING)

    def test_shows_plain_confirm_choice_alongside_whatsapp(self):
        """O admin pode decidir se quer avisar o cliente ou só confirmar."""
        client_ = Client.objects.create(tenant=self.tenant, phone="11988887777", name="Maria")
        appointment = book(
            self.tenant, self.employee, self.service, client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/{appointment.pk}/confirmar/preparar/")
        body = response.content.decode()
        self.assertIn("Só confirmar", body)
        self.assertIn("Confirmar no WhatsApp", body)

    def test_no_valid_phone_shows_notice_without_message_field(self):
        client_ = Client.objects.create(
            tenant=self.tenant, phone="removido-1", name="Cliente removido (LGPD)"
        )
        appointment = book(
            self.tenant, self.employee, self.service, client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/{appointment.pk}/confirmar/preparar/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn("wa.me/", body)
        self.assertNotIn("Só confirmar", body)
        self.assertIn("não tem um WhatsApp válido", body)

    def test_confirm_button_in_modal_actually_confirms(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11988887777", name="Maria")
        appointment = book(
            self.tenant, self.employee, self.service, client_,
            self.monday, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/confirmar/?date={self.monday.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)


class AgendaPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        from apps.employees.services import link_service, set_working_hours

        link_service(cls.employee, cls.service)
        set_working_hours(
            cls.employee,
            [{"weekday": wd, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)} for wd in range(7)],
        )
        cls.today = datetime.date.today()

    def test_login_required(self):
        response = self.client.get("/painel/agenda/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=self.tenant
        )
        self.client.force_login(employee_user)
        response = self.client.get("/painel/agenda/")
        self.assertEqual(response.status_code, 403)

    def test_agenda_lists_appointments_for_selected_date(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/?date={tomorrow.isoformat()}")
        self.assertContains(response, "Corte")
        self.assertContains(response, "Cliente Teste")

    def test_card_has_client_preferences_button(self):
        """Botão pra ler as observações do cliente antes de iniciar o
        atendimento (alergia, preferências etc. cadastradas em Clientes)."""
        tomorrow = self.today + datetime.timedelta(days=1)
        book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.CONFIRMED,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/?date={tomorrow.isoformat()}")
        self.assertContains(response, f"/painel/clientes/{self.client_.pk}/preferencias/")

    def test_confirm_action(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/confirmar/?date={tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_start_action_via_htmx(self):
        """Iniciar Atendimento (confirmado → em atendimento) — a partir daqui
        a comanda aparece no Caixa, não mais um botão Concluir na Agenda."""
        tomorrow = self.today + datetime.timedelta(days=1)
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.CONFIRMED,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/iniciar/?date={tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.IN_PROGRESS)

    def test_start_action_from_pending_via_htmx(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/iniciar/?date={tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.IN_PROGRESS)

    def test_cannot_start_already_in_progress_appointment(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.IN_PROGRESS,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/iniciar/?date={tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 409)

    def test_in_progress_appointment_has_no_cancel_or_no_show_button(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.IN_PROGRESS,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/?date={tomorrow.isoformat()}")
        self.assertContains(response, "Em Atendimento")
        self.assertContains(response, "Finalize a comanda no Caixa")
        self.assertNotContains(response, "Iniciar Atendimento")

    def test_concluir_route_no_longer_exists(self):
        """A conclusão de atendimento saiu da Agenda — agora só pelo Caixa
        (aba Comandas, ver apps/finance)."""
        tomorrow = self.today + datetime.timedelta(days=1)
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.CONFIRMED,
        )
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/agenda/{appointment.pk}/concluir/")
        self.assertEqual(response.status_code, 404)

    def test_cancel_confirm_modal_has_no_native_confirm(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(
            f"/painel/agenda/{appointment.pk}/cancelar/confirmar/?date={tomorrow.isoformat()}"
        )
        self.assertContains(response, "Cancelar agendamento")
        self.assertNotContains(response, "hx-confirm")

    def test_manual_appointment_creation(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/agenda/novo/",
            {
                "service": self.service.pk, "employee": self.employee.pk,
                "date": tomorrow.isoformat(), "time": "14:00",
                "phone": "11955554444", "name": "Cliente Balcão", "notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        appointment = Appointment.objects.get(client__phone="11955554444")
        self.assertEqual(appointment.employee, self.employee)
        self.assertEqual(appointment.created_by, self.admin)

    def test_new_form_shows_placeholder_before_selecting_service_employee_date(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/agenda/novo/")
        self.assertContains(response, "Selecione serviço, profissional e data")

    def test_slots_endpoint_returns_available_times_synced_with_public_availability(self):
        """RF17: o mesmo motor de disponibilidade da página pública
        (apps/scheduling/availability.py) tem que valer aqui — sem duplicar
        cálculo de horário livre (regra 5 do CLAUDE.md)."""
        tomorrow = self.today + datetime.timedelta(days=1)
        self.client.force_login(self.admin)
        response = self.client.get(
            "/painel/agenda/novo/horarios/",
            {
                "service": self.service.pk,
                "employee": self.employee.pk,
                "date": tomorrow.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "09:00")
        self.assertNotContains(response, "Selecione serviço")

    def test_slots_endpoint_excludes_already_booked_time(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.CONFIRMED,
        )
        self.client.force_login(self.admin)
        response = self.client.get(
            "/painel/agenda/novo/horarios/",
            {
                "service": self.service.pk,
                "employee": self.employee.pk,
                "date": tomorrow.isoformat(),
            },
        )
        self.assertNotContains(response, "09:00")
        self.assertContains(response, "10:00")

    def test_slots_endpoint_missing_params_shows_placeholder(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            "/painel/agenda/novo/horarios/", {"service": self.service.pk}
        )
        self.assertContains(response, "Selecione serviço, profissional e data")

    def test_manual_creation_rejects_slot_taken_between_render_and_submit(self):
        """Mesma proteção contra corrida da página pública (unique constraint
        + revalidação em `create_appointment`) — vale também pro encaixe manual."""
        tomorrow = self.today + datetime.timedelta(days=1)
        book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(14, 0), datetime.time(15, 0), status=AppointmentStatus.CONFIRMED,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/agenda/novo/",
            {
                "service": self.service.pk, "employee": self.employee.pk,
                "date": tomorrow.isoformat(), "time": "14:00",
                "phone": "11955554444", "name": "Cliente Balcão", "notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Appointment.objects.filter(client__phone="11955554444").exists())

    def test_isolation_cannot_act_on_other_tenant_appointment(self):
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        other_employee = make_employee(other_tenant, email="bia@salao-b.com", full_name="Bia")
        other_client = make_client(other_tenant, phone="+5511777770000", name="Outro Cliente")
        other_service = create_service(
            tenant=other_tenant, name="Corte B", duration_minutes=60, price=Decimal("50")
        )
        tomorrow = self.today + datetime.timedelta(days=1)
        other_appointment = book(
            other_tenant, other_employee, other_service, other_client,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/agenda/{other_appointment.pk}/confirmar/?date={tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 404)


class CanceledByClientBadgeAndPollingTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-canceladopelocliente")
        cls.employee = make_employee(cls.tenant)
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        cls.today = datetime.date.today()

    def test_items_view_shows_client_cancel_badge(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.CANCELED,
        )
        appointment.canceled_by_client = True
        appointment.save(update_fields=["canceled_by_client"])
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/?date={tomorrow.isoformat()}")
        self.assertContains(response, "Cancelado pelo cliente")

    def test_items_view_shows_plain_canceled_for_admin_cancel(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.CANCELED,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/?date={tomorrow.isoformat()}")
        self.assertContains(response, ">Cancelado<")
        self.assertNotContains(response, "Cancelado pelo cliente")

    def test_agenda_items_poll_requires_login(self):
        response = self.client.get("/painel/agenda/atualizar/")
        self.assertEqual(response.status_code, 302)

    def test_agenda_items_poll_returns_current_items(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        book(
            self.tenant, self.employee, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/atualizar/?date={tomorrow.isoformat()}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Corte")
        # não deve fechar modal aberto (sem o reset de #modal-slot)
        self.assertNotContains(response, 'hx-swap-oob="true"')

    def test_agenda_week_poll_returns_grid(self):
        self.client.force_login(self.admin)
        week_monday = self.today - datetime.timedelta(days=self.today.weekday())
        response = self.client.get(f"/painel/agenda/semana/atualizar/?week={week_monday.isoformat()}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="agenda-week-grid"')


class AgendaWeekPanelTest(TestCase):
    """Visão semanal (estilo Google Calendar) — grade de 7 dias com filtro
    por funcionário, pedido do usuário em 2026-07-29."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.other_employee = make_employee(
            cls.tenant, email="bia@salao-a.com", full_name="Bia Souza"
        )
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        cls.today = datetime.date.today()

    def test_login_required(self):
        response = self.client.get("/painel/agenda/semana/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=self.tenant
        )
        self.client.force_login(employee_user)
        response = self.client.get("/painel/agenda/semana/")
        self.assertEqual(response.status_code, 403)

    def test_week_view_lists_appointment_in_grid(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.today, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/semana/?week={self.today.isoformat()}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Corte")
        self.assertContains(response, "Cliente Teste")
        self.assertContains(
            response, f"/painel/agenda/{appointment.pk}/detalhe/"
        )

    def test_week_view_employee_filter_excludes_other_employees(self):
        book(
            self.tenant, self.employee, self.service, self.client_,
            self.today, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        other_client = make_client(self.tenant, phone="+5511888880000", name="Outra Cliente")
        book(
            self.tenant, self.other_employee, self.service, other_client,
            self.today, datetime.time(11, 0), datetime.time(12, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(
            f"/painel/agenda/semana/?week={self.today.isoformat()}&employee={self.employee.pk}"
        )
        self.assertContains(response, "Cliente Teste")
        self.assertNotContains(response, "Outra Cliente")

    def test_week_navigation_moves_by_seven_days(self):
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/semana/?week={self.today.isoformat()}")
        # semana começa no Domingo (decisão do usuário em 2026-08-04) —
        # date.weekday() do Python é Seg=0..Dom=6, por isso o +1 % 7.
        sunday = self.today - datetime.timedelta(days=(self.today.weekday() + 1) % 7)
        self.assertContains(response, f"week={(sunday + datetime.timedelta(days=7)).isoformat()}")
        self.assertContains(response, f"week={(sunday - datetime.timedelta(days=7)).isoformat()}")

    def test_week_starts_on_sunday(self):
        """A grade lista Dom→Sáb, não Seg→Dom (decisão do usuário em
        2026-08-04) — confere a ordem exibindo o nome dos dias na resposta."""
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/semana/?week={self.today.isoformat()}")
        body = response.content.decode()
        sunday = self.today - datetime.timedelta(days=(self.today.weekday() + 1) % 7)
        self.assertContains(response, f"{sunday.strftime('%d/%m')} – {(sunday + datetime.timedelta(days=6)).strftime('%d/%m/%Y')}")
        dom_idx = body.index(">Dom<")
        sab_idx = body.index(">Sáb<")
        self.assertLess(dom_idx, sab_idx, "Domingo deve aparecer antes de Sábado na grade")

    def test_closed_day_marked_in_grid(self):
        """Dia marcado como fechado em Configurações fica visualmente
        diferenciado na grade da semana, mas continua listado (regra 5 do
        CLAUDE.md: `TenantBusinessHours` é só informativo, não bloqueia
        agendamento — um funcionário pode ter jornada num dia "fechado")."""
        from apps.tenants.models import TenantBusinessHours, Weekday

        sunday = self.today - datetime.timedelta(days=(self.today.weekday() + 1) % 7)
        TenantBusinessHours.objects.create(
            tenant=self.tenant, weekday=Weekday.SUNDAY, is_closed=True
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/semana/?week={sunday.isoformat()}")
        self.assertContains(response, "Fechado")
        self.assertContains(response, sunday.strftime("%d/%m"))

    def test_appointment_detail_modal_renders(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.today, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(
            f"/painel/agenda/{appointment.pk}/detalhe/?view=week&week={self.today.isoformat()}"
        )
        self.assertContains(response, "Cliente Teste")
        self.assertContains(response, "Confirmar")
        self.assertContains(response, f"/painel/clientes/{self.client_.pk}/preferencias/")

    def test_confirm_from_week_view_refreshes_week_grid(self):
        appointment = book(
            self.tenant, self.employee, self.service, self.client_,
            self.today, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/confirmar/?view=week&week={self.today.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="agenda-week-grid"')
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_isolation_other_tenant_appointment_not_in_week_grid(self):
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        other_employee = make_employee(other_tenant, email="c@salao-b.com", full_name="Carla")
        other_client = make_client(other_tenant, phone="+5511777770000", name="Cliente B")
        other_service = create_service(
            tenant=other_tenant, name="Corte B", duration_minutes=60, price=Decimal("50")
        )
        book(
            other_tenant, other_employee, other_service, other_client,
            self.today, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/agenda/semana/?week={self.today.isoformat()}")
        self.assertNotContains(response, "Cliente B")


class MyAgendaTest(TestCase):
    """RF12 — só os próprios agendamentos do funcionário, somente leitura."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.ana = make_employee(cls.tenant, email="ana@salao-a.com", full_name="Ana")
        cls.bia = make_employee(cls.tenant, email="bia@salao-a.com", full_name="Bia")
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        cls.today = datetime.date.today()

    def test_login_required(self):
        response = self.client.get("/painel/minha-agenda/")
        self.assertEqual(response.status_code, 302)

    def test_employee_sees_only_own_appointments(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        book(
            self.tenant, self.ana, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.CONFIRMED,
        )
        book(
            self.tenant, self.bia, self.service, self.client_,
            tomorrow, datetime.time(11, 0), datetime.time(12, 0), status=AppointmentStatus.CONFIRMED,
        )
        self.client.force_login(self.ana.user)
        response = self.client.get(f"/painel/minha-agenda/?date={tomorrow.isoformat()}")
        self.assertContains(response, "09:00")
        self.assertNotContains(response, "11:00")

    def test_read_only_no_action_buttons(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        book(
            self.tenant, self.ana, self.service, self.client_,
            tomorrow, datetime.time(9, 0), datetime.time(10, 0), status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.ana.user)
        response = self.client.get(f"/painel/minha-agenda/?date={tomorrow.isoformat()}")
        self.assertNotContains(response, "Confirmar")
        self.assertNotContains(response, "hx-post")

    def test_admin_without_employee_profile_forbidden(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/minha-agenda/")
        self.assertEqual(response.status_code, 403)


class EmployeeSchedulingPermissionsTest(TestCase):
    """Autonomia do funcionário na própria agenda (decisão do usuário em
    2026-08-07) — 3 toggles independentes em `Tenant`, desligados por
    padrão; cada ação só vale pro PRÓPRIO agendamento do funcionário, nunca
    de um colega (admin nunca tem essa restrição)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-permissoes")
        cls.ana = make_employee(cls.tenant, email="ana@salao-permissoes.com", full_name="Ana")
        cls.bia = make_employee(cls.tenant, email="bia@salao-permissoes.com", full_name="Bia")
        cls.client_ = make_client(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        cls.tomorrow = datetime.date.today() + datetime.timedelta(days=1)

    def _new_appointment_payload(self, employee, date, **overrides):
        data = {
            "service": self.service.pk,
            "employee": employee.pk,
            "date": date.isoformat(),
            "time": "09:00",
            "phone": "11912345678",
            "name": "Cliente Novo",
            "birth_day": "",
            "birth_month": "",
            "notes": "",
        }
        data.update(overrides)
        return data

    # -- confirmar ----------------------------------------------------

    def test_confirm_blocked_when_flag_off(self):
        appointment = book(
            self.tenant, self.ana, self.service, self.client_,
            self.tomorrow, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.ana.user)
        response = self.client.post(f"/painel/agenda/{appointment.pk}/confirmar/")
        self.assertEqual(response.status_code, 403)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.PENDING)

    def test_confirm_allowed_on_own_appointment_when_flag_on(self):
        self.tenant.employee_can_confirm_appointments = True
        self.tenant.save(update_fields=["employee_can_confirm_appointments"])
        appointment = book(
            self.tenant, self.ana, self.service, self.client_,
            self.tomorrow, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.ana.user)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/confirmar/?date={self.tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_confirm_blocked_on_colleagues_appointment_even_when_flag_on(self):
        self.tenant.employee_can_confirm_appointments = True
        self.tenant.save(update_fields=["employee_can_confirm_appointments"])
        appointment = book(
            self.tenant, self.bia, self.service, self.client_,
            self.tomorrow, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.ana.user)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/confirmar/?date={self.tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 403)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.PENDING)

    # -- iniciar atendimento -------------------------------------------

    def test_start_blocked_when_flag_off(self):
        appointment = book(
            self.tenant, self.ana, self.service, self.client_,
            self.tomorrow, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.CONFIRMED,
        )
        self.client.force_login(self.ana.user)
        response = self.client.post(f"/painel/agenda/{appointment.pk}/iniciar/")
        self.assertEqual(response.status_code, 403)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_start_allowed_on_own_appointment_when_flag_on(self):
        self.tenant.employee_can_start_appointments = True
        self.tenant.save(update_fields=["employee_can_start_appointments"])
        appointment = book(
            self.tenant, self.ana, self.service, self.client_,
            self.tomorrow, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.CONFIRMED,
        )
        self.client.force_login(self.ana.user)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/iniciar/?date={self.tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.IN_PROGRESS)

    def test_start_blocked_on_colleagues_appointment_even_when_flag_on(self):
        self.tenant.employee_can_start_appointments = True
        self.tenant.save(update_fields=["employee_can_start_appointments"])
        appointment = book(
            self.tenant, self.bia, self.service, self.client_,
            self.tomorrow, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.CONFIRMED,
        )
        self.client.force_login(self.ana.user)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/iniciar/?date={self.tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 403)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    # -- agendar ---------------------------------------------------------

    def test_new_appointment_blocked_when_flag_off(self):
        self.client.force_login(self.ana.user)
        response = self.client.get("/painel/agenda/novo/")
        self.assertEqual(response.status_code, 403)

    def test_new_appointment_form_locks_employee_field_when_flag_on(self):
        self.tenant.employee_can_create_appointments = True
        self.tenant.save(update_fields=["employee_can_create_appointments"])
        self.client.force_login(self.ana.user)
        response = self.client.get("/painel/agenda/novo/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Ana", body)
        self.assertNotIn("Bia", body)

    def test_new_appointment_rejects_tampered_employee_field(self):
        """Mesmo que o funcionário force outro `employee` no POST, o form só
        aceita ele mesmo — o queryset já vem travado em
        `NewAppointmentForm.__init__` (`ModelChoiceField` rejeita valor fora
        do queryset)."""
        self.tenant.employee_can_create_appointments = True
        self.tenant.save(update_fields=["employee_can_create_appointments"])
        monday = next_weekday(self.tomorrow, 0)
        set_working_hours(
            self.ana,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}],
        )
        self.client.force_login(self.ana.user)
        response = self.client.post(
            "/painel/agenda/novo/", self._new_appointment_payload(self.bia, monday)
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Appointment.objects.filter(employee=self.bia).exists())

    def test_new_appointment_creates_for_self_when_flag_on(self):
        self.tenant.employee_can_create_appointments = True
        self.tenant.save(update_fields=["employee_can_create_appointments"])
        monday = next_weekday(self.tomorrow, 0)
        set_working_hours(
            self.ana,
            [{"weekday": 0, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}],
        )
        self.client.force_login(self.ana.user)
        response = self.client.post(
            "/painel/agenda/novo/", self._new_appointment_payload(self.ana, monday)
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Appointment.objects.filter(employee=self.ana, client__phone="11912345678").exists()
        )

    # -- admin nunca é afetado / "Minha Agenda" mostra os botões --------

    def test_admin_always_allowed_regardless_of_flags(self):
        appointment = book(
            self.tenant, self.ana, self.service, self.client_,
            self.tomorrow, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/agenda/{appointment.pk}/confirmar/?date={self.tomorrow.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

    def test_my_agenda_shows_action_buttons_when_flags_on(self):
        self.tenant.employee_can_confirm_appointments = True
        self.tenant.employee_can_start_appointments = True
        self.tenant.employee_can_create_appointments = True
        self.tenant.save(update_fields=[
            "employee_can_confirm_appointments",
            "employee_can_start_appointments",
            "employee_can_create_appointments",
        ])
        book(
            self.tenant, self.ana, self.service, self.client_,
            self.tomorrow, datetime.time(9, 0), datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
        )
        self.client.force_login(self.ana.user)
        response = self.client.get(f"/painel/minha-agenda/?date={self.tomorrow.isoformat()}")
        self.assertContains(response, "Confirmar")
        self.assertContains(response, "Iniciar Atendimento")
        self.assertContains(response, "Novo Agendamento")


class PackageCoverageTest(TestCase):
    """Pacote de mensalidade cobrindo o serviço (decisão do usuário em
    2026-08-04): agendamento nasce com `package` marcado; `price_at_booking`
    continua o valor de tabela (base de comissão), mas `complete_appointment`
    não cobra o cliente de novo — a mensalidade já foi paga na hora que o
    pacote foi atribuído (`assign_package_to_client`)."""

    @classmethod
    def setUpTestData(cls):
        from apps.clients.services import assign_package_to_client, create_package

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.corte = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.escova = create_service(
            tenant=cls.tenant, name="Escova", duration_minutes=30, price=Decimal("50.00")
        )
        link_service(cls.employee, cls.corte)
        link_service(cls.employee, cls.escova)
        set_working_hours(
            cls.employee,
            [{"weekday": wd, "start_time": datetime.time(8, 0), "end_time": datetime.time(20, 0)} for wd in range(7)],
        )
        cls.client_ = make_client(cls.tenant)

    def _package(self, *, generates_commission=True, services=None):
        from apps.clients.services import create_package

        return create_package(
            tenant=self.tenant, name="Cabelo Ilimitado", price=Decimal("150.00"),
            service_ids=[s.pk for s in (services or [self.corte])],
            generates_commission=generates_commission, created_by=self.admin,
        )

    def _subscribe(self, package):
        from apps.clients.services import assign_package_to_client

        assign_package_to_client(
            self.client_, package=package, payment_method="pix", created_by=self.admin,
        )
        self.client_.refresh_from_db()

    def test_appointment_snapshots_package_when_service_covered(self):
        package = self._package()
        self._subscribe(package)
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        appointment = create_appointment(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.corte, date=tomorrow, start_time=datetime.time(9, 0),
        )
        self.assertEqual(appointment.package, package)
        # valor de tabela continua intacto — não é zerado no agendamento
        self.assertEqual(appointment.price_at_booking, Decimal("100.00"))

    def test_appointment_not_covered_when_service_not_in_package(self):
        package = self._package(services=[self.corte])  # não inclui escova
        self._subscribe(package)
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        appointment = create_appointment(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.escova, date=tomorrow, start_time=datetime.time(9, 0),
        )
        self.assertIsNone(appointment.package)

    def test_appointment_not_covered_when_client_not_subscriber(self):
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        appointment = create_appointment(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.corte, date=tomorrow, start_time=datetime.time(9, 0),
        )
        self.assertIsNone(appointment.package)

    def test_walk_in_service_also_detects_package_coverage(self):
        package = self._package()
        self._subscribe(package)
        appointment = start_walk_in_service(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.corte, created_by=self.admin,
        )
        self.assertEqual(appointment.package, package)

    def test_completing_covered_appointment_skips_cash_transaction_for_service(self):
        package = self._package()
        self._subscribe(package)
        appointment = start_walk_in_service(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.corte, created_by=self.admin,
        )
        complete_appointment(appointment=appointment, payment_method="cash", created_by=self.admin)
        self.assertFalse(
            CashTransaction.objects.filter(
                tenant=self.tenant, category=CashCategory.SERVICE_SALE, related_appointment=appointment,
            ).exists()
        )

    def test_completing_covered_appointment_with_commission_enabled_still_pays_commission(self):
        package = self._package(generates_commission=True)
        self._subscribe(package)
        appointment = start_walk_in_service(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.corte, created_by=self.admin,
        )
        commission = complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin
        )
        self.assertIsNotNone(commission)
        # 40% (padrão de make_employee) sobre o valor de TABELA do serviço (100)
        self.assertEqual(commission.calculated_amount, Decimal("40.00"))
        self.assertEqual(commission.base_amount, Decimal("100.00"))

    def test_completing_covered_appointment_with_commission_disabled_generates_no_commission(self):
        package = self._package(generates_commission=False)
        self._subscribe(package)
        appointment = start_walk_in_service(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.corte, created_by=self.admin,
        )
        commission = complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin
        )
        self.assertIsNone(commission)
        self.assertFalse(Commission.objects.filter(appointment=appointment).exists())

    def test_covered_appointment_with_product_still_charges_product(self):
        from apps.inventory.services import register_stock_movement

        package = self._package()
        self._subscribe(package)
        product = create_product(
            tenant=self.tenant, name="Pomada", unit="un",
            cost_price=Decimal("5.00"), sale_price=Decimal("20.00"), min_stock_alert=Decimal("1"),
        )
        register_stock_movement(
            tenant=self.tenant, product=product, movement_type="in",
            quantity=Decimal("10"), unit_price=Decimal("1.00"), reason="purchase",
            created_by=self.admin,
        )
        appointment = start_walk_in_service(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.corte, created_by=self.admin,
        )
        complete_appointment(
            appointment=appointment, payment_method="cash", created_by=self.admin,
            product_usage=[{"product": product, "quantity": Decimal("1"), "unit_price": product.sale_price}],
        )
        product_txn = CashTransaction.objects.get(
            tenant=self.tenant, category=CashCategory.PRODUCT_SALE, related_appointment=appointment,
        )
        self.assertEqual(product_txn.amount, Decimal("20.00"))
        self.assertFalse(
            CashTransaction.objects.filter(
                tenant=self.tenant, category=CashCategory.SERVICE_SALE, related_appointment=appointment,
            ).exists()
        )

    def test_mixed_comanda_only_charges_the_non_covered_service(self):
        """Cliente faz corte (coberto pelo pacote) + escova (não coberta) na
        mesma comanda — só a escova entra na cobrança. Dois profissionais
        diferentes (não só pra variar) evita colidir com a trava de
        "um atendimento em andamento por vez" do walk-in, que é por
        employee+date+start_time — dois walk-in do MESMO profissional no
        mesmo segundo colidiriam."""
        other_employee = make_employee(self.tenant, email="bia@salao-a.com", full_name="Bia")
        link_service(other_employee, self.escova)
        package = self._package(services=[self.corte])
        self._subscribe(package)
        corte_appt = start_walk_in_service(
            tenant=self.tenant, client=self.client_, employee=self.employee,
            service=self.corte, created_by=self.admin,
        )
        escova_appt = start_walk_in_service(
            tenant=self.tenant, client=self.client_, employee=other_employee,
            service=self.escova, created_by=self.admin,
        )
        self.assertIsNotNone(corte_appt.package)
        self.assertIsNone(escova_appt.package)

        complete_client_comanda(
            appointments=[corte_appt, escova_appt], payment_method="cash", created_by=self.admin,
        )
        service_txns = CashTransaction.objects.filter(
            tenant=self.tenant, category=CashCategory.SERVICE_SALE,
        )
        self.assertEqual(service_txns.count(), 1)
        self.assertEqual(service_txns.first().amount, Decimal("50.00"))
        self.assertTrue(Commission.objects.filter(appointment=corte_appt).exists())
        self.assertTrue(Commission.objects.filter(appointment=escova_appt).exists())
