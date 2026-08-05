import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.clients.models import Client
from apps.employees.services import create_employee
from apps.finance.services import sell_products
from apps.inventory.services import create_product
from apps.scheduling.models import Appointment, AppointmentStatus
from apps.scheduling.services import complete_appointment
from apps.services.services import create_service
from apps.tenants.models import Tenant

User = get_user_model()


def make_tenant_with_admin(slug):
    tenant = Tenant.objects.create(name=f"Salão {slug}", slug=slug)
    admin = User.objects.create_user(
        email=f"admin@{slug}.com", password="x", role=User.Role.TENANT_ADMIN, tenant=tenant
    )
    return tenant, admin


class ReportsAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )

    def test_login_required(self):
        response = self.client.get("/painel/relatorios/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee_user)
        response = self.client.get("/painel/relatorios/")
        self.assertEqual(response.status_code, 403)

    def test_admin_can_access(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        self.assertEqual(response.status_code, 200)

    def test_nav_link_present(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertContains(response, "/painel/relatorios/")


class ReportsDataTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="11999990000", name="Cliente Teste")
        cls.today = datetime.date.today()

    def _completed_appointment(self):
        appointment = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=self.today, start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )
        complete_appointment(appointment=appointment, payment_method="cash", created_by=self.admin)
        return appointment

    def _period_url(self):
        start = self.today.replace(day=1).isoformat()
        end = self.today.isoformat()
        return f"/painel/relatorios/?start={start}&end={end}"

    def test_revenue_and_dre_reflect_completed_appointment(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        response = self.client.get(self._period_url())
        self.assertContains(response, "R$ 100,00")  # entrada do DRE
        self.assertContains(response, "Corte")

    def test_product_sale_appears_in_top_products(self):
        product = create_product(
            tenant=self.tenant, name="Shampoo", unit="un",
            cost_price=Decimal("5"), sale_price=Decimal("20"), min_stock_alert=Decimal("1"),
        )
        from apps.inventory.services import register_stock_movement
        from apps.inventory.models import MovementType, MovementReason

        register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("5"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        sell_products(
            tenant=self.tenant,
            product_usage=[{"product": product, "quantity": Decimal("2"), "unit_price": Decimal("20")}],
            payment_method="cash", created_by=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.get(self._period_url())
        self.assertContains(response, "Shampoo")

    def test_period_outside_range_excludes_data(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        last_year = self.today.replace(year=self.today.year - 1)
        response = self.client.get(
            f"/painel/relatorios/?start={last_year.isoformat()}&end={last_year.isoformat()}"
        )
        self.assertNotContains(response, "R$ 100,00")

    def test_default_period_is_current_month(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        month_start = self.today.replace(day=1)
        self.assertContains(response, f'value="{month_start.isoformat()}"')
        self.assertContains(response, f'value="{self.today.isoformat()}"')


class ReportsIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")

    def test_other_tenant_service_not_shown(self):
        employee_b = create_employee(
            tenant=self.tenant_b, full_name="Beatriz", email="bia@salao-b.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("30"),
        )
        service_b = create_service(
            tenant=self.tenant_b, name="ServicoExclusivoB", duration_minutes=30, price=Decimal("50")
        )
        client_b = Client.objects.create(tenant=self.tenant_b, phone="11988887777", name="Cliente B")
        today = datetime.date.today()
        appointment = Appointment.objects.create(
            tenant=self.tenant_b, client=client_b, employee=employee_b, service=service_b,
            date=today, start_time=datetime.time(9, 0), end_time=datetime.time(9, 30),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("50.00"),
        )
        complete_appointment(appointment=appointment, payment_method="cash", created_by=self.admin_b)

        self.client.force_login(self.admin_a)
        start = today.replace(day=1).isoformat()
        response = self.client.get(f"/painel/relatorios/?start={start}&end={today.isoformat()}")
        self.assertNotContains(response, "ServicoExclusivoB")
        self.assertNotContains(response, "R$ 50,00")
