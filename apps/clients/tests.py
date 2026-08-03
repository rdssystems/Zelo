import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.tenants.models import Tenant

from .models import Client, ClientCreditTransaction
from .services import (
    _add_months,
    add_client_credit,
    anonymize_client,
    create_client,
    format_phone_display,
    get_or_create_client,
    normalize_phone,
    redeem_client_credit_for_appointment,
    remove_client_credit,
    renew_subscription,
    set_subscriber_status,
    update_client,
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
