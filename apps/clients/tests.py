import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.notifications.models import TenantNotification, TenantNotificationKind
from apps.tenants.models import Tenant

from .models import Client, ClientCreditTransaction, ClientDebtTransaction, Package
from .services import (
    _add_months,
    add_client_credit,
    anonymize_client,
    assign_package_to_client,
    clients_with_birthday_today,
    create_client,
    create_package,
    ensure_birthday_notification,
    find_client_by_phone,
    format_phone_display,
    get_or_create_client,
    mark_birthday_message_sent,
    normalize_phone,
    pending_birthday_clients_today,
    record_client_debt,
    redeem_client_credit_for_appointment,
    remove_client_credit,
    renew_subscription,
    set_package_active,
    set_subscriber_status,
    settle_client_debt,
    update_client,
    update_client_preferences,
    update_package,
    write_off_client_debt,
)

User = get_user_model()


class ClientModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.tenant_b = Tenant.objects.create(name="Salão B", slug="salao-b")

    def test_same_phone_allowed_across_different_tenants(self):
        Client.objects.create(tenant=self.tenant_a, phone="+5511999990000", name="Maria")
        # mesmo telefone em outro tenant não é conflito (regra 03-MODELO-DE-DADOS.md)
        Client.objects.create(tenant=self.tenant_b, phone="+5511999990000", name="Maria")
        self.assertEqual(Client.objects.count(), 2)

    def test_same_phone_rejected_within_same_tenant(self):
        Client.objects.create(tenant=self.tenant_a, phone="+5511999990000", name="Maria")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Client.objects.create(
                tenant=self.tenant_a, phone="+5511999990000", name="Outra Maria"
            )

    def test_isolation(self):
        Client.objects.create(tenant=self.tenant_a, phone="1", name="A")
        Client.objects.create(tenant=self.tenant_b, phone="2", name="B")
        names_a = list(
            Client.objects.for_tenant(self.tenant_a).values_list("name", flat=True)
        )
        self.assertEqual(names_a, ["A"])

    def test_whatsapp_url_for_valid_phone(self):
        client_ = Client.objects.create(tenant=self.tenant_a, phone="11988887777", name="Maria")
        self.assertEqual(client_.whatsapp_url, "https://wa.me/5511988887777")

    def test_net_balance_positive_when_credit_exceeds_debt(self):
        client_ = Client.objects.create(
            tenant=self.tenant_a, phone="11988887777", name="Maria",
            credit_balance=Decimal("50"), debt_balance=Decimal("20"),
        )
        self.assertEqual(client_.net_balance, Decimal("30"))

    def test_net_balance_negative_when_debt_exceeds_credit(self):
        client_ = Client.objects.create(
            tenant=self.tenant_a, phone="11988887777", name="Maria",
            credit_balance=Decimal("0"), debt_balance=Decimal("35"),
        )
        self.assertEqual(client_.net_balance, Decimal("-35"))

    def test_net_balance_zero_when_no_credit_or_debt(self):
        client_ = Client.objects.create(tenant=self.tenant_a, phone="11988887777", name="Maria")
        self.assertEqual(client_.net_balance, Decimal("0"))

    def test_whatsapp_url_none_for_anonymized_phone(self):
        client_ = Client.objects.create(tenant=self.tenant_a, phone="removido-1", name="Cliente removido (LGPD)")
        self.assertIsNone(client_.whatsapp_url)


class NormalizePhoneTest(TestCase):
    def test_formatted_br_number(self):
        self.assertEqual(normalize_phone("(11) 91234-5678"), "11912345678")

    def test_landline_10_digits(self):
        self.assertEqual(normalize_phone("(11) 1234-5678"), "1112345678")

    def test_strips_country_code_55(self):
        self.assertEqual(normalize_phone("+55 11 91234-5678"), "11912345678")
        self.assertEqual(normalize_phone("5511912345678"), "11912345678")

    def test_too_short_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_phone("123456")

    def test_too_long_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_phone("119123456789999")

    def test_empty_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_phone("")


class FormatPhoneDisplayTest(TestCase):
    def test_mobile_11_digits(self):
        self.assertEqual(format_phone_display("11912345678"), "(11) 91234-5678")

    def test_landline_10_digits(self):
        self.assertEqual(format_phone_display("1112345678"), "(11) 1234-5678")


class FindClientByPhoneTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")

    def test_finds_existing_client_by_formatted_phone(self):
        client = Client.objects.create(tenant=self.tenant, phone="11912345678", name="Maria")
        found = find_client_by_phone(self.tenant, "(11) 91234-5678")
        self.assertEqual(found, client)

    def test_returns_none_when_not_found(self):
        self.assertIsNone(find_client_by_phone(self.tenant, "11912345678"))

    def test_returns_none_for_invalid_phone_instead_of_raising(self):
        self.assertIsNone(find_client_by_phone(self.tenant, "123"))

    def test_isolated_per_tenant(self):
        other_tenant = Tenant.objects.create(name="Salão B", slug="salao-b")
        Client.objects.create(tenant=other_tenant, phone="11912345678", name="Maria")
        self.assertIsNone(find_client_by_phone(self.tenant, "11912345678"))


class GetOrCreateClientTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")

    def test_new_phone_without_name_raises(self):
        with self.assertRaises(ValidationError):
            get_or_create_client(tenant=self.tenant, phone="(11) 91234-5678")
        self.assertEqual(Client.objects.count(), 0)

    def test_new_phone_with_name_creates(self):
        client, created = get_or_create_client(
            tenant=self.tenant, phone="(11) 91234-5678", name="  Maria  "
        )
        self.assertTrue(created)
        self.assertEqual(client.name, "Maria")
        self.assertEqual(client.phone, "11912345678")

    def test_existing_phone_recovers_ignoring_new_name(self):
        existing = Client.objects.create(
            tenant=self.tenant, phone="11912345678", name="Maria Original"
        )
        client, created = get_or_create_client(
            tenant=self.tenant, phone="(11) 91234-5678", name="Nome Diferente"
        )
        self.assertFalse(created)
        self.assertEqual(client.pk, existing.pk)
        self.assertEqual(client.name, "Maria Original")
        self.assertEqual(Client.objects.count(), 1)

    def test_new_client_with_birthday_saves_day_and_month(self):
        client, created = get_or_create_client(
            tenant=self.tenant, phone="11912345678", name="Maria",
            birth_day=15, birth_month=6,
        )
        self.assertTrue(created)
        self.assertEqual(client.birth_day, 15)
        self.assertEqual(client.birth_month, 6)

    def test_new_client_without_birthday_leaves_it_blank(self):
        client, created = get_or_create_client(
            tenant=self.tenant, phone="11912345678", name="Maria",
        )
        self.assertIsNone(client.birth_day)
        self.assertIsNone(client.birth_month)

    def test_new_client_with_only_day_rejected(self):
        with self.assertRaises(ValidationError):
            get_or_create_client(
                tenant=self.tenant, phone="11912345678", name="Maria", birth_day=15,
            )
        self.assertEqual(Client.objects.count(), 0)

    def test_new_client_with_invalid_day_for_month_rejected(self):
        with self.assertRaises(ValidationError):
            get_or_create_client(
                tenant=self.tenant, phone="11912345678", name="Maria",
                birth_day=31, birth_month=4,
            )

    def test_existing_client_recovery_ignores_new_birthday(self):
        existing = Client.objects.create(
            tenant=self.tenant, phone="11912345678", name="Maria",
            birth_day=1, birth_month=1,
        )
        client, created = get_or_create_client(
            tenant=self.tenant, phone="11912345678", birth_day=25, birth_month=12,
        )
        self.assertFalse(created)
        self.assertEqual(client.pk, existing.pk)
        self.assertEqual(client.birth_day, 1)
        self.assertEqual(client.birth_month, 1)


class RequireBirthdayOnBookingTest(TestCase):
    """`Tenant.require_birthday_on_booking` — aniversário obrigatório no
    cadastro de cliente novo (agendamento público e painel), configurável
    por tenant."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(
            name="Salão A", slug="salao-a", require_birthday_on_booking=True
        )
        cls.tenant_off = Tenant.objects.create(
            name="Salão B", slug="salao-b", require_birthday_on_booking=False
        )

    def test_get_or_create_client_new_phone_without_birthday_rejected(self):
        with self.assertRaises(ValidationError):
            get_or_create_client(
                tenant=self.tenant, phone="11912345678", name="Maria",
            )
        self.assertEqual(Client.objects.count(), 0)

    def test_get_or_create_client_new_phone_with_birthday_succeeds(self):
        client, created = get_or_create_client(
            tenant=self.tenant, phone="11912345678", name="Maria",
            birth_day=15, birth_month=6,
        )
        self.assertTrue(created)
        self.assertEqual(client.birth_day, 15)

    def test_tenant_without_flag_unaffected(self):
        client, created = get_or_create_client(
            tenant=self.tenant_off, phone="11912345678", name="Maria",
        )
        self.assertTrue(created)
        self.assertIsNone(client.birth_day)

    def test_existing_client_recovery_not_blocked_even_without_birthday(self):
        existing = Client.objects.create(
            tenant=self.tenant, phone="11912345678", name="Maria",
        )
        client, created = get_or_create_client(
            tenant=self.tenant, phone="11912345678",
        )
        self.assertFalse(created)
        self.assertEqual(client.pk, existing.pk)

    def test_create_client_without_birthday_rejected(self):
        with self.assertRaises(ValidationError):
            create_client(tenant=self.tenant, name="Maria", phone="11912345678")
        self.assertEqual(Client.objects.count(), 0)

    def test_create_client_with_birthday_succeeds(self):
        client = create_client(
            tenant=self.tenant, name="Maria", phone="11912345678",
            birth_day=15, birth_month=6,
        )
        self.assertEqual(client.birth_day, 15)

    def test_update_client_without_birthday_rejected(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11912345678", name="Maria",
        )
        with self.assertRaises(ValidationError):
            update_client(client, name="Maria Silva", phone="11912345678")

    def test_update_client_with_birthday_succeeds(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11912345678", name="Maria",
        )
        updated = update_client(
            client, name="Maria Silva", phone="11912345678",
            birth_day=15, birth_month=6,
        )
        self.assertEqual(updated.birth_day, 15)


class AnonymizeClientTest(TestCase):
    """RNF07 (LGPD): cliente final pode pedir a exclusão dos seus dados."""

    @classmethod
    def setUpTestData(cls):
        from apps.employees.services import create_employee, link_service, set_working_hours
        from apps.services.services import create_service

        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.employee = create_employee(
            tenant=cls.tenant, full_name="Ana", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40"),
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        link_service(cls.employee, cls.service)
        set_working_hours(
            cls.employee,
            [{"weekday": wd, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)} for wd in range(7)],
        )

    def test_removes_name_and_phone(self):
        client = Client.objects.create(tenant=self.tenant, phone="11912345678", name="Maria")
        anonymize_client(client)
        client.refresh_from_db()
        self.assertNotEqual(client.name, "Maria")
        self.assertNotEqual(client.phone, "11912345678")

    def test_keeps_row_for_audit_trail(self):
        """Não pode ser um hard delete — Appointment.client é PROTECT."""
        client = Client.objects.create(tenant=self.tenant, phone="11912345678", name="Maria")
        pk = client.pk
        anonymize_client(client)
        self.assertTrue(Client.objects.filter(pk=pk).exists())

    def test_clears_birthday(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11912345678", name="Maria", birth_day=10, birth_month=5,
        )
        anonymize_client(client)
        client.refresh_from_db()
        self.assertIsNone(client.birth_day)
        self.assertIsNone(client.birth_month)

    def test_cancels_future_pending_and_confirmed_appointments(self):
        from apps.scheduling.services import create_appointment

        client = Client.objects.create(tenant=self.tenant, phone="11912345678", name="Maria")
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        appointment = create_appointment(
            tenant=self.tenant, client=client, employee=self.employee,
            service=self.service, date=tomorrow, start_time=datetime.time(9, 0),
        )
        anonymize_client(client)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "canceled")

    def test_does_not_touch_completed_appointment_history(self):
        from apps.scheduling.models import Appointment, AppointmentStatus

        client = Client.objects.create(tenant=self.tenant, phone="11912345678", name="Maria")
        appointment = Appointment.objects.create(
            tenant=self.tenant, client=client, employee=self.employee, service=self.service,
            date=datetime.date.today(), start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.COMPLETED, price_at_booking=Decimal("100"),
        )
        anonymize_client(client)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)

    def test_preserves_credit_balance_and_subscription(self):
        """Decisão de política: saldo/mensalista não são dado pessoal — só
        nome/telefone/preferências somem na anonimização LGPD."""
        client = Client.objects.create(
            tenant=self.tenant, phone="11912345678", name="Maria",
            preferences="Alérgica a amônia", is_subscriber=True,
            subscription_due_date=datetime.date.today(),
        )
        add_client_credit(
            client, amount=Decimal("50"), payment_method="cash", created_by=None
        )
        anonymize_client(client)
        client.refresh_from_db()
        self.assertEqual(client.credit_balance, Decimal("50"))
        self.assertTrue(client.is_subscriber)
        self.assertIsNotNone(client.subscription_due_date)
        self.assertEqual(client.preferences, "")


class DeleteMyDataPublicFlowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.client_ = Client.objects.create(
            tenant=cls.tenant, phone="11912345678", name="Maria"
        )

    def _verify_phone(self):
        return self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11912345678"}
        )

    def test_requires_phone_verification_first(self):
        response = self.client.get(
            f"/{self.tenant.slug}/meus-agendamentos/excluir-dados/confirmar/"
        )
        self.assertEqual(response.status_code, 404)

    def test_confirm_modal_renders_after_verification(self):
        self._verify_phone()
        response = self.client.get(
            f"/{self.tenant.slug}/meus-agendamentos/excluir-dados/confirmar/"
        )
        self.assertContains(response, "Excluir meus dados")
        self.assertNotContains(response, "hx-confirm")

    def test_delete_anonymizes_and_clears_session(self):
        self._verify_phone()
        response = self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/excluir-dados/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "removidos")
        self.client_.refresh_from_db()
        self.assertNotEqual(self.client_.name, "Maria")

        # sessão foi limpa — não dá pra reabrir o fluxo sem verificar de novo
        response = self.client.get(
            f"/{self.tenant.slug}/meus-agendamentos/excluir-dados/confirmar/"
        )
        self.assertEqual(response.status_code, 404)

    def test_cannot_delete_without_verification(self):
        response = self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/excluir-dados/"
        )
        self.assertEqual(response.status_code, 404)
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.name, "Maria")


def make_tenant_with_admin(slug):
    tenant = Tenant.objects.create(name=f"Salão {slug}", slug=slug)
    admin = User.objects.create_user(
        email=f"admin@{slug}.com", password="x", role=User.Role.TENANT_ADMIN, tenant=tenant
    )
    return tenant, admin


class AddMonthsTest(TestCase):
    """Vira ano, mês curto (fev) — sem depender de python-dateutil."""

    def test_simple_month_add(self):
        self.assertEqual(_add_months(datetime.date(2026, 3, 10), 1), datetime.date(2026, 4, 10))

    def test_year_rollover(self):
        self.assertEqual(_add_months(datetime.date(2026, 12, 15), 1), datetime.date(2027, 1, 15))

    def test_clamps_to_shorter_month(self):
        self.assertEqual(_add_months(datetime.date(2026, 1, 31), 1), datetime.date(2026, 2, 28))

    def test_clamps_leap_year_february(self):
        self.assertEqual(_add_months(datetime.date(2028, 1, 31), 1), datetime.date(2028, 2, 29))


class SubscriptionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_enable_requires_due_date(self):
        client = Client.objects.create(tenant=self.tenant, phone="11900000000", name="Maria")
        with self.assertRaises(ValidationError):
            set_subscriber_status(client, is_subscriber=True, due_date=None)

    def test_enable_with_due_date(self):
        client = Client.objects.create(tenant=self.tenant, phone="11900000000", name="Maria")
        due = datetime.date.today() + datetime.timedelta(days=30)
        set_subscriber_status(client, is_subscriber=True, due_date=due)
        client.refresh_from_db()
        self.assertTrue(client.is_subscriber)
        self.assertEqual(client.subscription_due_date, due)

    def test_disable_clears_due_date(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria",
            is_subscriber=True, subscription_due_date=datetime.date.today(),
        )
        set_subscriber_status(client, is_subscriber=False)
        client.refresh_from_db()
        self.assertFalse(client.is_subscriber)
        self.assertIsNone(client.subscription_due_date)

    def test_overdue_property(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", is_subscriber=True,
            subscription_due_date=datetime.date.today() - datetime.timedelta(days=1),
        )
        self.assertTrue(client.subscription_is_overdue)
        self.assertFalse(client.subscription_is_due_soon)

    def test_due_soon_property(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", is_subscriber=True,
            subscription_due_date=datetime.date.today() + datetime.timedelta(days=3),
        )
        self.assertTrue(client.subscription_is_due_soon)
        self.assertFalse(client.subscription_is_overdue)

    def test_renew_pushes_one_month_from_due_date_when_not_overdue(self):
        due = datetime.date.today() + datetime.timedelta(days=10)
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria",
            is_subscriber=True, subscription_due_date=due,
        )
        renew_subscription(client)
        client.refresh_from_db()
        self.assertEqual(client.subscription_due_date, _add_months(due, 1))

    def test_renew_pushes_one_month_from_today_when_overdue(self):
        overdue = datetime.date.today() - datetime.timedelta(days=40)
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria",
            is_subscriber=True, subscription_due_date=overdue,
        )
        renew_subscription(client)
        client.refresh_from_db()
        self.assertEqual(client.subscription_due_date, _add_months(datetime.date.today(), 1))

    def test_renew_non_subscriber_rejected(self):
        client = Client.objects.create(tenant=self.tenant, phone="11900000000", name="Maria")
        with self.assertRaises(ValidationError):
            renew_subscription(client)


class CreditWalletTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_add_credit_creates_real_cash_transaction(self):
        from apps.finance.models import CashCategory, CashFlowType, CashTransaction

        client = Client.objects.create(tenant=self.tenant, phone="11900000000", name="Maria")
        add_client_credit(
            client, amount=Decimal("100"), payment_method="pix", created_by=self.admin
        )
        client.refresh_from_db()
        self.assertEqual(client.credit_balance, Decimal("100"))

        txn = CashTransaction.objects.get(tenant=self.tenant)
        self.assertEqual(txn.type, CashFlowType.IN)
        self.assertEqual(txn.category, CashCategory.CLIENT_CREDIT_TOPUP)
        self.assertEqual(txn.amount, Decimal("100"))

        entry = ClientCreditTransaction.objects.get(client=client)
        self.assertEqual(entry.type, CashFlowType.IN)
        self.assertEqual(entry.related_cash_transaction, txn)

    def test_add_credit_rejects_client_credit_as_payment_method(self):
        client = Client.objects.create(tenant=self.tenant, phone="11900000000", name="Maria")
        with self.assertRaises(ValidationError):
            add_client_credit(
                client, amount=Decimal("10"), payment_method="client_credit",
                created_by=self.admin,
            )

    def test_remove_credit_does_not_touch_cash(self):
        from apps.finance.models import CashTransaction

        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", credit_balance=Decimal("100")
        )
        remove_client_credit(client, amount=Decimal("30"), created_by=self.admin)
        client.refresh_from_db()
        self.assertEqual(client.credit_balance, Decimal("70"))
        self.assertEqual(CashTransaction.objects.count(), 0)
        entry = ClientCreditTransaction.objects.get(client=client)
        self.assertEqual(entry.type, "out")
        self.assertIsNone(entry.related_cash_transaction)

    def test_remove_credit_insufficient_balance_rejected(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", credit_balance=Decimal("10")
        )
        with self.assertRaises(ValidationError):
            remove_client_credit(client, amount=Decimal("50"), created_by=self.admin)
        client.refresh_from_db()
        self.assertEqual(client.credit_balance, Decimal("10"))

    def test_redeem_for_appointment_does_not_create_cash_transaction(self):
        from apps.employees.services import create_employee
        from apps.finance.models import CashTransaction
        from apps.scheduling.models import Appointment, AppointmentStatus
        from apps.services.services import create_service

        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", credit_balance=Decimal("100")
        )
        employee = create_employee(
            tenant=self.tenant, full_name="Ana", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40"),
        )
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("80")
        )
        appointment = Appointment.objects.create(
            tenant=self.tenant, client=client, employee=employee, service=service,
            date=datetime.date.today(), start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("80"),
        )
        redeem_client_credit_for_appointment(
            client, amount=Decimal("80"), appointment=appointment, created_by=self.admin
        )
        client.refresh_from_db()
        self.assertEqual(client.credit_balance, Decimal("20"))
        self.assertEqual(CashTransaction.objects.count(), 0)
        entry = ClientCreditTransaction.objects.get(client=client)
        self.assertEqual(entry.related_appointment, appointment)

    def test_redeem_insufficient_credit_rejected(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", credit_balance=Decimal("10")
        )
        with self.assertRaises(ValidationError):
            redeem_client_credit_for_appointment(
                client, amount=Decimal("50"), appointment=None, created_by=self.admin
            )


class DebtLedgerTest(TestCase):
    """Carteira de saldo devedor (fiado) — espelha `CreditWalletTest`, com a
    polaridade invertida (IN = passou a dever mais, OUT = quitou/ajustou)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_record_debt_increases_balance_without_cash_transaction(self):
        from apps.employees.services import create_employee
        from apps.finance.models import CashTransaction
        from apps.scheduling.models import Appointment, AppointmentStatus
        from apps.services.services import create_service

        client = Client.objects.create(tenant=self.tenant, phone="11900000000", name="Maria")
        employee = create_employee(
            tenant=self.tenant, full_name="Ana", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40"),
        )
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("80")
        )
        appointment = Appointment.objects.create(
            tenant=self.tenant, client=client, employee=employee, service=service,
            date=datetime.date.today(), start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("80"),
        )
        record_client_debt(
            client, amount=Decimal("30"), appointment=appointment, created_by=self.admin
        )
        client.refresh_from_db()
        self.assertEqual(client.debt_balance, Decimal("30"))
        self.assertEqual(CashTransaction.objects.count(), 0)
        entry = ClientDebtTransaction.objects.get(client=client)
        self.assertEqual(entry.type, "in")
        self.assertEqual(entry.related_appointment, appointment)
        self.assertIsNone(entry.related_cash_transaction)

    def test_settle_debt_creates_real_cash_transaction(self):
        from apps.finance.models import CashCategory, CashFlowType, CashTransaction

        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", debt_balance=Decimal("50")
        )
        settle_client_debt(
            client, amount=Decimal("50"), payment_method="pix", created_by=self.admin
        )
        client.refresh_from_db()
        self.assertEqual(client.debt_balance, Decimal("0"))

        txn = CashTransaction.objects.get(tenant=self.tenant)
        self.assertEqual(txn.type, CashFlowType.IN)
        self.assertEqual(txn.category, CashCategory.CLIENT_DEBT_PAYMENT)
        self.assertEqual(txn.amount, Decimal("50"))

        entry = ClientDebtTransaction.objects.get(client=client)
        self.assertEqual(entry.type, "out")
        self.assertEqual(entry.related_cash_transaction, txn)

    def test_settle_debt_rejects_client_credit_as_payment_method(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", debt_balance=Decimal("50")
        )
        with self.assertRaises(ValidationError):
            settle_client_debt(
                client, amount=Decimal("10"), payment_method="client_credit",
                created_by=self.admin,
            )

    def test_settle_debt_insufficient_balance_rejected(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", debt_balance=Decimal("10")
        )
        with self.assertRaises(ValidationError):
            settle_client_debt(
                client, amount=Decimal("50"), payment_method="pix", created_by=self.admin
            )
        client.refresh_from_db()
        self.assertEqual(client.debt_balance, Decimal("10"))

    def test_write_off_debt_does_not_touch_cash(self):
        from apps.finance.models import CashTransaction

        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", debt_balance=Decimal("50")
        )
        write_off_client_debt(client, amount=Decimal("20"), created_by=self.admin)
        client.refresh_from_db()
        self.assertEqual(client.debt_balance, Decimal("30"))
        self.assertEqual(CashTransaction.objects.count(), 0)
        entry = ClientDebtTransaction.objects.get(client=client)
        self.assertEqual(entry.type, "out")
        self.assertIsNone(entry.related_cash_transaction)

    def test_write_off_debt_insufficient_balance_rejected(self):
        client = Client.objects.create(
            tenant=self.tenant, phone="11900000000", name="Maria", debt_balance=Decimal("10")
        )
        with self.assertRaises(ValidationError):
            write_off_client_debt(client, amount=Decimal("50"), created_by=self.admin)

    def test_debt_isolated_per_tenant(self):
        tenant_b, admin_b = make_tenant_with_admin("salao-b")
        client_a = Client.objects.create(tenant=self.tenant, phone="11900000000", name="Maria")
        record_client_debt(client_a, amount=Decimal("40"), appointment=None, created_by=self.admin)
        self.assertEqual(ClientDebtTransaction.objects.for_tenant(tenant_b).count(), 0)
        self.assertEqual(Client.objects.for_tenant(tenant_b).filter(debt_balance__gt=0).count(), 0)


class ClientCrudDomainTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_create_client(self):
        client = create_client(
            tenant=self.tenant, name="Maria", phone="(11) 91234-5678", preferences="Sem cheiro forte"
        )
        self.assertEqual(client.phone, "11912345678")
        self.assertEqual(client.preferences, "Sem cheiro forte")

    def test_create_duplicate_phone_rejected(self):
        create_client(tenant=self.tenant, name="Maria", phone="11912345678")
        with self.assertRaises(ValidationError):
            create_client(tenant=self.tenant, name="Outra", phone="11912345678")

    def test_update_client(self):
        client = create_client(tenant=self.tenant, name="Maria", phone="11912345678")
        update_client(client, name="Maria Silva", phone="11988887777", preferences="Alérgica a látex")
        client.refresh_from_db()
        self.assertEqual(client.name, "Maria Silva")
        self.assertEqual(client.phone, "11988887777")
        self.assertEqual(client.preferences, "Alérgica a látex")

    def test_update_to_phone_already_used_by_another_client_rejected(self):
        create_client(tenant=self.tenant, name="Maria", phone="11912345678")
        other = create_client(tenant=self.tenant, name="Outra", phone="11988887777")
        with self.assertRaises(ValidationError):
            update_client(other, name="Outra", phone="11912345678")

    def test_update_client_preferences_does_not_touch_name_or_phone(self):
        client = create_client(tenant=self.tenant, name="Maria", phone="11912345678")
        update_client_preferences(client, "Alérgica a amônia")
        client.refresh_from_db()
        self.assertEqual(client.preferences, "Alérgica a amônia")
        self.assertEqual(client.name, "Maria")
        self.assertEqual(client.phone, "11912345678")

    def test_update_client_preferences_strips_and_allows_blank(self):
        client = create_client(tenant=self.tenant, name="Maria", phone="11912345678", preferences="Antigo")
        update_client_preferences(client, "  ")
        client.refresh_from_db()
        self.assertEqual(client.preferences, "")

    def test_create_client_with_birthday(self):
        client = create_client(
            tenant=self.tenant, name="Maria", phone="11912345678",
            birth_day=29, birth_month=2,
        )
        self.assertEqual(client.birth_day, 29)
        self.assertEqual(client.birth_month, 2)

    def test_create_client_with_invalid_month_rejected(self):
        with self.assertRaises(ValidationError):
            create_client(
                tenant=self.tenant, name="Maria", phone="11912345678",
                birth_day=1, birth_month=13,
            )

    def test_update_client_can_add_and_clear_birthday(self):
        client = create_client(tenant=self.tenant, name="Maria", phone="11912345678")
        update_client(client, name="Maria", phone="11912345678", birth_day=10, birth_month=3)
        client.refresh_from_db()
        self.assertEqual(client.birth_day, 10)
        self.assertEqual(client.birth_month, 3)

        update_client(client, name="Maria", phone="11912345678")
        client.refresh_from_db()
        self.assertIsNone(client.birth_day)
        self.assertIsNone(client.birth_month)


class ClientPreferencesModalPanelTest(TestCase):
    """Botão "ver observações" na Agenda (RF) e o atalho de editar preferências
    a partir do prompt pós-finalização de comanda no Caixa."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.client_ = Client.objects.create(
            tenant=cls.tenant, phone="11988887777", name="Maria",
            preferences="Alérgica a amônia",
        )
        cls.other_tenant, cls.other_admin = make_tenant_with_admin("salao-b")
        cls.other_client = Client.objects.create(
            tenant=cls.other_tenant, phone="11933334444", name="Bia"
        )

    def test_login_required(self):
        response = self.client.get(f"/painel/clientes/{self.client_.pk}/preferencias/")
        self.assertEqual(response.status_code, 302)

    def test_read_modal_shows_preferences(self):
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/clientes/{self.client_.pk}/preferencias/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alérgica a amônia")

    def test_read_modal_empty_state(self):
        empty_client = Client.objects.create(tenant=self.tenant, phone="11955556666", name="Joana")
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/clientes/{empty_client.pk}/preferencias/")
        self.assertContains(response, "Nenhuma observação registrada")

    def test_read_modal_scoped_to_tenant(self):
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/clientes/{self.other_client.pk}/preferencias/")
        self.assertEqual(response.status_code, 404)

    def test_edit_modal_prefills_current_text(self):
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/clientes/{self.client_.pk}/preferencias/editar/")
        self.assertContains(response, "Alérgica a amônia")

    def test_update_saves_and_shows_confirmation(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{self.client_.pk}/preferencias/atualizar/",
            {"preferences": "Prefere tesoura, não usa máquina"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Preferências atualizadas.")
        self.assertContains(response, "Prefere tesoura, não usa máquina")
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.preferences, "Prefere tesoura, não usa máquina")

    def test_update_scoped_to_tenant(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{self.other_client.pk}/preferencias/atualizar/",
            {"preferences": "Invasão"},
        )
        self.assertEqual(response.status_code, 404)
        self.other_client.refresh_from_db()
        self.assertEqual(self.other_client.preferences, "")

    def test_update_requires_post(self):
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/clientes/{self.client_.pk}/preferencias/atualizar/")
        self.assertEqual(response.status_code, 405)


class ClientPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )

    def test_login_required(self):
        response = self.client.get("/painel/clientes/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee_user)
        response = self.client.get("/painel/clientes/")
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_client_via_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/clientes/novo/",
            {"name": "Maria", "phone": "(11) 91234-5678", "preferences": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Client.objects.filter(tenant=self.tenant, name="Maria").exists())

    def test_list_shows_saldo_column_green_when_positive(self):
        Client.objects.create(
            tenant=self.tenant, phone="11988887777", name="Maria",
            credit_balance=Decimal("50"), debt_balance=Decimal("20"),
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/")
        body = response.content.decode()
        self.assertIn("Saldo", body)
        self.assertNotIn(">Crédito<", body)
        self.assertIn("text-[#2e7d32]", body)
        self.assertIn("R$ 30", body)

    def test_list_shows_saldo_column_red_when_negative(self):
        Client.objects.create(
            tenant=self.tenant, phone="11988887777", name="Maria",
            credit_balance=Decimal("0"), debt_balance=Decimal("35"),
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/")
        body = response.content.decode()
        self.assertIn("text-error", body)
        self.assertIn("-R$ 35", body)

    def test_deleted_client_disappears_from_list(self):
        """Regressão: o modal de exclusão promete "ele some da lista", mas o
        registro anonimizado (LGPD) continuava aparecendo como "Cliente
        removido (LGPD)" — a lista precisa filtrar esse caso."""
        client_ = Client.objects.create(tenant=self.tenant, phone="11988887777", name="Maria")
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/clientes/{client_.pk}/excluir/")
        self.assertEqual(response.status_code, 200)
        client_.refresh_from_db()
        self.assertTrue(client_.phone.startswith("removido-"))

        response = self.client.get("/painel/clientes/")
        self.assertNotContains(response, "Cliente removido (LGPD)")

    def test_admin_sets_birthday_via_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/clientes/novo/",
            {"name": "Maria", "phone": "(11) 91234-5678", "preferences": "",
             "birth_day": "20", "birth_month": "9"},
        )
        self.assertEqual(response.status_code, 302)
        client_ = Client.objects.get(tenant=self.tenant, name="Maria")
        self.assertEqual(client_.birth_day, 20)
        self.assertEqual(client_.birth_month, 9)

    def test_admin_edits_birthday_via_panel(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11911112222", name="Ana")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{client_.pk}/editar/",
            {"name": "Ana", "phone": "11911112222", "preferences": "",
             "birth_day": "3", "birth_month": "11"},
        )
        self.assertEqual(response.status_code, 302)
        client_.refresh_from_db()
        self.assertEqual(client_.birth_day, 3)
        self.assertEqual(client_.birth_month, 11)

    def test_search_by_name_or_phone(self):
        Client.objects.create(tenant=self.tenant, phone="11911112222", name="Ana Souza")
        Client.objects.create(tenant=self.tenant, phone="11933334444", name="Bruna Lima")
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/?q=Ana")
        self.assertContains(response, "Ana Souza")
        self.assertNotContains(response, "Bruna Lima")

    def test_subscription_toggle_via_htmx(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11911112222", name="Ana")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{client_.pk}/mensalista/",
            {"is_subscriber": "on", "subscription_due_date": "2026-12-10"},
        )
        self.assertEqual(response.status_code, 200)
        client_.refresh_from_db()
        self.assertTrue(client_.is_subscriber)
        self.assertEqual(client_.subscription_due_date, datetime.date(2026, 12, 10))

    def test_subscription_without_due_date_shows_error(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11911112222", name="Ana")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{client_.pk}/mensalista/", {"is_subscriber": "on"}
        )
        self.assertContains(response, "vencimento")
        client_.refresh_from_db()
        self.assertFalse(client_.is_subscriber)

    def test_credit_add_via_htmx(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11911112222", name="Ana")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{client_.pk}/credito/creditar/",
            {"amount": "50,00", "payment_method": "pix"},
        )
        self.assertEqual(response.status_code, 200)
        client_.refresh_from_db()
        self.assertEqual(client_.credit_balance, Decimal("50.00"))

    def test_credit_remove_via_htmx(self):
        client_ = Client.objects.create(
            tenant=self.tenant, phone="11911112222", name="Ana", credit_balance=Decimal("50")
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{client_.pk}/credito/remover/",
            {"amount": "20,00", "reason": "Correção"},
        )
        self.assertEqual(response.status_code, 200)
        client_.refresh_from_db()
        self.assertEqual(client_.credit_balance, Decimal("30.00"))

    def test_credit_remove_insufficient_balance_shows_error(self):
        client_ = Client.objects.create(
            tenant=self.tenant, phone="11911112222", name="Ana", credit_balance=Decimal("10")
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{client_.pk}/credito/remover/", {"amount": "50,00"}
        )
        self.assertContains(response, "Saldo insuficiente")
        client_.refresh_from_db()
        self.assertEqual(client_.credit_balance, Decimal("10"))

    def test_delete_confirm_warns_history_is_preserved(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11911112222", name="Ana")
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/clientes/{client_.pk}/excluir/confirmar/")
        self.assertContains(response, "Ana")
        self.assertContains(response, "NÃO é afetado")

    def test_delete_anonymizes_client_and_preserves_credit_balance(self):
        """Excluir cliente no painel usa a mesma anonimização LGPD (não some
        a linha — `Appointment.client` é PROTECT) — nome/telefone somem, mas
        saldo de crédito e histórico financeiro continuam intactos."""
        client_ = Client.objects.create(
            tenant=self.tenant, phone="11911112222", name="Ana", credit_balance=Decimal("50")
        )
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/clientes/{client_.pk}/excluir/")
        self.assertEqual(response.headers.get("HX-Redirect"), "/painel/clientes/")
        client_.refresh_from_db()
        self.assertEqual(client_.name, "Cliente removido (LGPD)")
        self.assertTrue(client_.phone.startswith("removido-"))
        self.assertEqual(client_.credit_balance, Decimal("50"))

    def test_deleted_client_no_longer_listed_by_original_name(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11911112222", name="Ana")
        self.client.force_login(self.admin)
        self.client.post(f"/painel/clientes/{client_.pk}/excluir/")
        response = self.client.get("/painel/clientes/")
        self.assertNotContains(response, "Ana")

    def test_whatsapp_column_links_to_wa_me(self):
        Client.objects.create(tenant=self.tenant, phone="11988887777", name="Ana")
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/")
        self.assertContains(response, "https://wa.me/5511988887777")


class ClientPanelIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")
        cls.client_a = Client.objects.create(tenant=cls.tenant_a, phone="11911112222", name="Ana")
        cls.client_b = Client.objects.create(tenant=cls.tenant_b, phone="11933334444", name="Bia")

    def test_panel_list_scoped(self):
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/clientes/")
        self.assertContains(response, "Ana")
        self.assertNotContains(response, "Bia")

    def test_panel_cannot_edit_other_tenant_client(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(f"/painel/clientes/{self.client_b.pk}/editar/")
        self.assertEqual(response.status_code, 404)

    def test_panel_cannot_delete_other_tenant_client(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(f"/painel/clientes/{self.client_b.pk}/excluir/")
        self.assertEqual(response.status_code, 404)
        self.client_b.refresh_from_db()
        self.assertEqual(self.client_b.name, "Bia")


class ClientFormAlpineRegressionTest(TestCase):
    """Mesma regressão já pega antes na aba Comandas: um bloco `x-data`
    desbalanceado quebra o Alpine.js inteiro sem gerar erro visível."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def _assert_all_x_data_balanced(self, body):
        idx = 0
        found = 0
        while True:
            marker = 'x-data="'
            start = body.find(marker, idx)
            if start == -1:
                break
            start += len(marker)
            end = body.index('"', start)
            js = body[start:end]
            self.assertEqual(js.count("["), js.count("]"), js)
            self.assertEqual(js.count("{"), js.count("}"), js)
            found += 1
            idx = end
        return found

    def test_client_form_x_data_brackets_are_balanced(self):
        client_ = Client.objects.create(
            tenant=self.tenant, phone="11911112222", name="Ana",
            is_subscriber=True, subscription_due_date=datetime.date.today(),
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/clientes/{client_.pk}/editar/")
        found = self._assert_all_x_data_balanced(response.content.decode())
        self.assertGreaterEqual(found, 1)


class DueSoonConfigurableWindowTest(TestCase):
    """`Client.subscription_is_due_soon` usava `7` fixo — agora lê
    `Tenant.subscription_due_soon_days` (Configurações)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_respects_shorter_configured_window(self):
        self.tenant.subscription_due_soon_days = 3
        self.tenant.save(update_fields=["subscription_due_soon_days"])
        client_ = Client.objects.create(
            tenant=self.tenant, phone="11900000001", name="Maria", is_subscriber=True,
            subscription_due_date=datetime.date.today() + datetime.timedelta(days=5),
        )
        self.assertFalse(client_.subscription_is_due_soon)

    def test_respects_longer_configured_window(self):
        self.tenant.subscription_due_soon_days = 15
        self.tenant.save(update_fields=["subscription_due_soon_days"])
        client_ = Client.objects.create(
            tenant=self.tenant, phone="11900000002", name="Joana", is_subscriber=True,
            subscription_due_date=datetime.date.today() + datetime.timedelta(days=10),
        )
        self.assertTrue(client_.subscription_is_due_soon)


class ClientInactiveTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.tenant.client_inactive_days = 30
        cls.tenant.save(update_fields=["client_inactive_days"])

    def test_recent_client_not_inactive(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11900000003", name="Bia")
        self.assertFalse(client_.is_inactive)

    def test_never_visited_client_uses_created_at_as_reference(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11900000004", name="Carla")
        Client.objects.filter(pk=client_.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=40)
        )
        client_.refresh_from_db()
        self.assertTrue(client_.is_inactive)

    def test_just_under_threshold_not_yet_inactive(self):
        client_ = Client.objects.create(tenant=self.tenant, phone="11900000005", name="Duda")
        Client.objects.filter(pk=client_.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=29)
        )
        client_.refresh_from_db()
        self.assertFalse(client_.is_inactive)


class SubscriptionWhatsAppCampaignPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")
        cls.overdue = Client.objects.create(
            tenant=cls.tenant_a, phone="11911110001", name="Vencida", is_subscriber=True,
            subscription_due_date=datetime.date.today() - datetime.timedelta(days=2),
        )
        cls.due_soon = Client.objects.create(
            tenant=cls.tenant_a, phone="11911110002", name="AVencer", is_subscriber=True,
            subscription_due_date=datetime.date.today() + datetime.timedelta(days=2),
        )
        cls.not_subscriber = Client.objects.create(
            tenant=cls.tenant_a, phone="11911110003", name="NaoMensalista",
        )
        cls.anonymized = Client.objects.create(
            tenant=cls.tenant_a, phone="removido-999", name="Cliente removido (LGPD)",
            is_subscriber=True, subscription_due_date=datetime.date.today() - datetime.timedelta(days=1),
        )
        cls.other_tenant_overdue = Client.objects.create(
            tenant=cls.tenant_b, phone="11922220001", name="OutroSalao", is_subscriber=True,
            subscription_due_date=datetime.date.today() - datetime.timedelta(days=2),
        )

    def test_login_required(self):
        response = self.client.get("/painel/clientes/mensalistas/whatsapp/")
        self.assertEqual(response.status_code, 302)

    def test_splits_overdue_and_due_soon_scoped_to_tenant(self):
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/clientes/mensalistas/whatsapp/")
        self.assertContains(response, "Vencida")
        self.assertContains(response, "AVencer")
        self.assertNotContains(response, "NaoMensalista")
        self.assertNotContains(response, "Cliente removido (LGPD)")
        self.assertNotContains(response, "OutroSalao")


class BirthdayAlertTest(TestCase):
    """RF: alerta de aniversário — decisão do usuário em 2026-08-04.
    `Tenant.birthday_alert_enabled` liga o alerta; a notificação (mesmo
    modelo `TenantNotification` já usado pra cancelamento de agendamento)
    só some quando a mensagem É ENVIADA de fato pra todo mundo pendente
    (clique em "Enviar" no modal), não só por abrir o modal — decisão do
    usuário em 2026-08-05."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.today = timezone.localdate()

    def _make_birthday_client(self, name="Maria", phone="11911112222", **overrides):
        return Client.objects.create(
            tenant=self.tenant, phone=phone, name=name,
            birth_day=self.today.day, birth_month=self.today.month, **overrides,
        )

    def test_clients_with_birthday_today_excludes_other_days(self):
        birthday_client = self._make_birthday_client()
        tomorrow = self.today + datetime.timedelta(days=1)
        Client.objects.create(
            tenant=self.tenant, phone="11933334444", name="Outra",
            birth_day=tomorrow.day, birth_month=tomorrow.month,
        )
        result = list(clients_with_birthday_today(self.tenant))
        self.assertEqual(result, [birthday_client])

    def test_ensure_notification_noop_when_alert_disabled(self):
        self._make_birthday_client()
        self.assertFalse(self.tenant.birthday_alert_enabled)
        notification = ensure_birthday_notification(self.tenant)
        self.assertIsNone(notification)
        self.assertEqual(TenantNotification.objects.filter(tenant=self.tenant).count(), 0)

    def test_ensure_notification_noop_without_birthday_clients(self):
        self.tenant.birthday_alert_enabled = True
        self.tenant.save(update_fields=["birthday_alert_enabled"])
        notification = ensure_birthday_notification(self.tenant)
        self.assertIsNone(notification)

    def test_ensure_notification_creates_when_enabled_and_birthday_today(self):
        self.tenant.birthday_alert_enabled = True
        self.tenant.save(update_fields=["birthday_alert_enabled"])
        client_ = self._make_birthday_client()
        notification = ensure_birthday_notification(self.tenant)
        self.assertIsNotNone(notification)
        self.assertEqual(notification.kind, TenantNotificationKind.BIRTHDAY_ALERT)
        self.assertIn(client_.name, notification.message)
        self.assertFalse(notification.is_read)

    def test_ensure_notification_idempotent_same_day(self):
        self.tenant.birthday_alert_enabled = True
        self.tenant.save(update_fields=["birthday_alert_enabled"])
        self._make_birthday_client()
        ensure_birthday_notification(self.tenant)
        ensure_birthday_notification(self.tenant)
        self.assertEqual(
            TenantNotification.objects.filter(
                tenant=self.tenant, kind=TenantNotificationKind.BIRTHDAY_ALERT
            ).count(),
            1,
        )

    def test_toast_poll_generates_birthday_notification(self):
        self.tenant.birthday_alert_enabled = True
        self.tenant.save(update_fields=["birthday_alert_enabled"])
        self._make_birthday_client(name="Aniversariante Hoje")
        self.client.force_login(self.admin)
        response = self.client.get("/painel/avisos/toast/")
        self.assertContains(response, "Aniversariante do dia")

    def test_login_required_for_campaign(self):
        response = self.client.get("/painel/clientes/aniversariantes/whatsapp/")
        self.assertEqual(response.status_code, 302)

    def test_campaign_lists_only_today_birthday_with_valid_phone(self):
        self._make_birthday_client(name="Maria", phone="11911112222")
        Client.objects.create(
            tenant=self.tenant, phone="removido-1", name="Sem WhatsApp válido",
            birth_day=self.today.day, birth_month=self.today.month,
        )
        tomorrow = self.today + datetime.timedelta(days=1)
        Client.objects.create(
            tenant=self.tenant, phone="11955556666", name="Outro Dia",
            birth_day=tomorrow.day, birth_month=tomorrow.month,
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/aniversariantes/whatsapp/")
        self.assertContains(response, "Maria")
        # a mensagem vai dentro de um json_script, o acento em "aniversário"
        # sai como ário — checa só a parte sem acento
        self.assertContains(response, "Feliz anivers")
        self.assertContains(response, "Toda a equipe do")
        self.assertNotContains(response, "Sem WhatsApp válido")
        self.assertNotContains(response, "Outro Dia")

    def test_opening_campaign_does_not_mark_birthday_notifications_read(self):
        self.tenant.birthday_alert_enabled = True
        self.tenant.save(update_fields=["birthday_alert_enabled"])
        self._make_birthday_client()
        notification = ensure_birthday_notification(self.tenant)
        self.assertFalse(notification.is_read)
        self.client.force_login(self.admin)
        self.client.get("/painel/clientes/aniversariantes/whatsapp/")
        notification.refresh_from_db()
        self.assertFalse(notification.is_read)

    def test_mark_birthday_message_sent_sets_greeted_date(self):
        client_ = self._make_birthday_client()
        self.assertIsNone(client_.last_birthday_greeted_on)
        mark_birthday_message_sent(client_)
        client_.refresh_from_db()
        self.assertEqual(client_.last_birthday_greeted_on, self.today)

    def test_pending_excludes_already_greeted_client(self):
        client_ = self._make_birthday_client()
        self.assertEqual(list(pending_birthday_clients_today(self.tenant)), [client_])
        mark_birthday_message_sent(client_)
        self.assertEqual(list(pending_birthday_clients_today(self.tenant)), [])

    def test_campaign_excludes_already_greeted_client(self):
        client_ = self._make_birthday_client(name="Maria")
        mark_birthday_message_sent(client_)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/aniversariantes/whatsapp/")
        self.assertNotContains(response, "Maria")

    def test_sending_last_pending_message_closes_notification(self):
        self.tenant.birthday_alert_enabled = True
        self.tenant.save(update_fields=["birthday_alert_enabled"])
        client_ = self._make_birthday_client()
        notification = ensure_birthday_notification(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/aniversariantes/whatsapp/{client_.pk}/enviar/"
        )
        self.assertEqual(response.status_code, 204)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

    def test_sending_message_for_one_of_two_keeps_notification_open(self):
        self.tenant.birthday_alert_enabled = True
        self.tenant.save(update_fields=["birthday_alert_enabled"])
        client_1 = self._make_birthday_client(name="Maria", phone="11911112222")
        self._make_birthday_client(name="Joana", phone="11933334444")
        notification = ensure_birthday_notification(self.tenant)
        self.client.force_login(self.admin)
        self.client.post(f"/painel/clientes/aniversariantes/whatsapp/{client_1.pk}/enviar/")
        notification.refresh_from_db()
        self.assertFalse(notification.is_read)

    def test_campaign_scoped_to_tenant(self):
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        Client.objects.create(
            tenant=other_tenant, phone="11911112222", name="ClienteOutroSalao",
            birth_day=self.today.day, birth_month=self.today.month,
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/aniversariantes/whatsapp/")
        self.assertNotContains(response, "ClienteOutroSalao")


class PackageDomainTest(TestCase):
    """Pacote de mensalidade — decisão do usuário em 2026-08-04: define quais
    serviços um cliente mensalista já tem inclusos e se isso ainda gera
    comissão pro funcionário. Camada em cima do mensalista tradicional."""

    @classmethod
    def setUpTestData(cls):
        from apps.services.services import create_service

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.corte = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.escova = create_service(
            tenant=cls.tenant, name="Escova", duration_minutes=30, price=Decimal("50.00")
        )

    def test_create_package(self):
        package = create_package(
            tenant=self.tenant, name="Cabelo Ilimitado", description="Corte + escova à vontade",
            price=Decimal("150.00"), service_ids=[self.corte.pk, self.escova.pk],
            generates_commission=True, created_by=self.admin,
        )
        self.assertEqual(package.name, "Cabelo Ilimitado")
        self.assertEqual(set(package.services.values_list("pk", flat=True)), {self.corte.pk, self.escova.pk})
        self.assertTrue(package.is_active)
        self.assertTrue(package.generates_commission)

    def test_create_package_requires_name(self):
        with self.assertRaises(ValidationError):
            create_package(
                tenant=self.tenant, name="", price=Decimal("150.00"),
                service_ids=[self.corte.pk], generates_commission=True, created_by=self.admin,
            )

    def test_create_package_requires_positive_price(self):
        with self.assertRaises(ValidationError):
            create_package(
                tenant=self.tenant, name="Pacote", price=Decimal("0"),
                service_ids=[self.corte.pk], generates_commission=True, created_by=self.admin,
            )

    def test_create_package_requires_at_least_one_service(self):
        with self.assertRaises(ValidationError):
            create_package(
                tenant=self.tenant, name="Pacote", price=Decimal("150.00"),
                service_ids=[], generates_commission=True, created_by=self.admin,
            )

    def test_create_package_ignores_service_from_other_tenant(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        from apps.services.services import create_service

        other_service = create_service(
            tenant=other_tenant, name="Corte Outro Salão", duration_minutes=60, price=Decimal("100")
        )
        with self.assertRaises(ValidationError):
            create_package(
                tenant=self.tenant, name="Pacote", price=Decimal("150.00"),
                service_ids=[other_service.pk], generates_commission=True, created_by=self.admin,
            )

    def test_update_package(self):
        package = create_package(
            tenant=self.tenant, name="Pacote", price=Decimal("150.00"),
            service_ids=[self.corte.pk], generates_commission=True, created_by=self.admin,
        )
        update_package(
            package, name="Pacote Renovado", description="Nova descrição", price=Decimal("200.00"),
            service_ids=[self.escova.pk], generates_commission=False,
        )
        package.refresh_from_db()
        self.assertEqual(package.name, "Pacote Renovado")
        self.assertEqual(package.price, Decimal("200.00"))
        self.assertEqual(list(package.services.values_list("pk", flat=True)), [self.escova.pk])
        self.assertFalse(package.generates_commission)

    def test_set_package_active_toggle(self):
        package = create_package(
            tenant=self.tenant, name="Pacote", price=Decimal("150.00"),
            service_ids=[self.corte.pk], generates_commission=True, created_by=self.admin,
        )
        set_package_active(package, False)
        package.refresh_from_db()
        self.assertFalse(package.is_active)


class AssignPackageToClientTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.services.services import create_service

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.corte = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.package = create_package(
            tenant=cls.tenant, name="Cabelo Ilimitado", price=Decimal("150.00"),
            service_ids=[cls.corte.pk], generates_commission=True, created_by=cls.admin,
        )
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="11911112222", name="Ana")

    def test_assign_creates_real_cash_transaction(self):
        from apps.finance.models import CashCategory, CashTransaction

        assign_package_to_client(
            self.client_, package=self.package, payment_method="pix", created_by=self.admin,
        )
        txn = CashTransaction.objects.get(tenant=self.tenant, category=CashCategory.PACKAGE_SALE)
        self.assertEqual(txn.amount, Decimal("150.00"))
        self.assertEqual(txn.payment_method, "pix")

    def test_assign_marks_subscriber_and_sets_due_date_plus_one_month(self):
        today = datetime.date.today()
        assign_package_to_client(
            self.client_, package=self.package, payment_method="pix", created_by=self.admin,
        )
        self.client_.refresh_from_db()
        self.assertTrue(self.client_.is_subscriber)
        self.assertEqual(self.client_.package, self.package)
        self.assertEqual(self.client_.subscription_due_date, _add_months(today, 1))

    def test_assign_rejects_package_from_other_tenant(self):
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        other_client = Client.objects.create(tenant=other_tenant, phone="11933334444", name="Bia")
        with self.assertRaises(ValidationError):
            assign_package_to_client(
                other_client, package=self.package, payment_method="pix", created_by=other_admin,
            )

    def test_assign_rejects_inactive_package(self):
        set_package_active(self.package, False)
        with self.assertRaises(ValidationError):
            assign_package_to_client(
                self.client_, package=self.package, payment_method="pix", created_by=self.admin,
            )

    def test_turning_off_subscriber_clears_package(self):
        assign_package_to_client(
            self.client_, package=self.package, payment_method="pix", created_by=self.admin,
        )
        self.client_.refresh_from_db()
        self.assertIsNotNone(self.client_.package)
        set_subscriber_status(self.client_, is_subscriber=False)
        self.client_.refresh_from_db()
        self.assertIsNone(self.client_.package)


class RenewSubscriptionWithPackageTest(TestCase):
    """Renovar mensalidade de cliente com pacote ativo gera uma NOVA cobrança
    no Caixa (decisão do usuário em 2026-08-04) — sem pacote, continua sendo
    só um controle de data (comportamento original, sem cobrança)."""

    @classmethod
    def setUpTestData(cls):
        from apps.services.services import create_service

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.corte = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.package = create_package(
            tenant=cls.tenant, name="Cabelo Ilimitado", price=Decimal("150.00"),
            service_ids=[cls.corte.pk], generates_commission=True, created_by=cls.admin,
        )
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="11911112222", name="Ana")

    def test_renew_without_package_does_not_charge(self):
        from apps.finance.models import CashCategory, CashTransaction

        set_subscriber_status(
            self.client_, is_subscriber=True, due_date=datetime.date.today()
        )
        renew_subscription(self.client_)
        self.assertFalse(
            CashTransaction.objects.filter(tenant=self.tenant, category=CashCategory.PACKAGE_SALE).exists()
        )

    def test_renew_with_package_requires_payment_method(self):
        assign_package_to_client(
            self.client_, package=self.package, payment_method="pix", created_by=self.admin,
        )
        self.client_.refresh_from_db()
        with self.assertRaises(ValidationError):
            renew_subscription(self.client_)

    def test_renew_with_package_creates_new_cash_transaction(self):
        from apps.finance.models import CashCategory, CashTransaction

        assign_package_to_client(
            self.client_, package=self.package, payment_method="pix", created_by=self.admin,
        )
        self.client_.refresh_from_db()
        renew_subscription(self.client_, payment_method="cash", created_by=self.admin)
        txns = CashTransaction.objects.filter(tenant=self.tenant, category=CashCategory.PACKAGE_SALE)
        # 1 da atribuição inicial + 1 da renovação (ordering padrão é
        # -created_at, então a mais recente é a primeira)
        self.assertEqual(txns.count(), 2)
        self.assertEqual(txns.first().payment_method, "cash")
        self.assertEqual(txns.first().amount, Decimal("150.00"))

    def test_renew_with_package_still_extends_due_date(self):
        assign_package_to_client(
            self.client_, package=self.package, payment_method="pix", created_by=self.admin,
        )
        self.client_.refresh_from_db()
        due_before = self.client_.subscription_due_date
        renew_subscription(self.client_, payment_method="cash", created_by=self.admin)
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.subscription_due_date, _add_months(due_before, 1))

    def test_renew_panel_flow_for_package_client(self):
        from apps.finance.models import CashCategory, CashTransaction

        assign_package_to_client(
            self.client_, package=self.package, payment_method="pix", created_by=self.admin,
        )
        self.client_.refresh_from_db()
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/clientes/{self.client_.pk}/mensalista/renovar/confirmar/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cabelo Ilimitado")

        response = self.client.post(
            f"/painel/clientes/{self.client_.pk}/mensalista/renovar/",
            {"payment_method": "pix"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            CashTransaction.objects.filter(tenant=self.tenant, category=CashCategory.PACKAGE_SALE).count(),
            2,
        )


class PackagePanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.services.services import create_service

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.corte = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="11911112222", name="Ana")

    def test_login_required(self):
        response = self.client.get("/painel/clientes/pacotes/")
        self.assertEqual(response.status_code, 302)

    def test_admin_creates_package_via_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/clientes/pacotes/novo/",
            {
                "name": "Cabelo Ilimitado", "description": "", "price": "150,00",
                "services": [self.corte.pk], "generates_commission": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Package.objects.filter(tenant=self.tenant, name="Cabelo Ilimitado").exists())

    def test_package_list_scoped_to_tenant(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        Package.objects.create(
            tenant=other_tenant, name="Pacote Outro Salão", price=Decimal("99.00"),
        )
        create_package(
            tenant=self.tenant, name="Meu Pacote", price=Decimal("150.00"),
            service_ids=[self.corte.pk], generates_commission=True, created_by=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/pacotes/")
        self.assertContains(response, "Meu Pacote")
        self.assertNotContains(response, "Pacote Outro Salão")

    def test_assign_package_flow_via_panel(self):
        from apps.finance.models import CashCategory, CashTransaction

        package = create_package(
            tenant=self.tenant, name="Cabelo Ilimitado", price=Decimal("150.00"),
            service_ids=[self.corte.pk], generates_commission=True, created_by=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{self.client_.pk}/mensalista/pacote/",
            {"package": package.pk, "payment_method": "cash"},
        )
        self.assertEqual(response.status_code, 200)
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.package, package)
        self.assertTrue(self.client_.is_subscriber)
        self.assertTrue(
            CashTransaction.objects.filter(
                tenant=self.tenant, category=CashCategory.PACKAGE_SALE, amount=Decimal("150.00")
            ).exists()
        )

    def test_assign_package_scoped_to_tenant(self):
        """Pacote de outro tenant nem existe pro form (queryset já filtra por
        tenant) — 404, mesmo padrão de `_get_client`/`_get_package`."""
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        other_package = Package.objects.create(
            tenant=other_tenant, name="Pacote Outro Salão", price=Decimal("99.00"), is_active=True,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/clientes/{self.client_.pk}/mensalista/pacote/",
            {"package": other_package.pk, "payment_method": "cash"},
        )
        self.assertEqual(response.status_code, 404)
        self.client_.refresh_from_db()
        self.assertIsNone(self.client_.package)
