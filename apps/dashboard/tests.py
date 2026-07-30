import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.clients.models import Client
from apps.clients.services import add_client_credit
from apps.employees.services import create_employee
from apps.finance.services import create_expense
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


class DashboardAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )

    def test_login_required(self):
        response = self.client.get("/painel/dashboard/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee_user)
        response = self.client.get("/painel/dashboard/")
        self.assertEqual(response.status_code, 403)

    def test_admin_can_access(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertEqual(response.status_code, 200)

    def test_nav_link_points_to_dashboard(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, '/painel/dashboard/')
        self.assertNotContains(response, 'href="#"')


class DashboardMetricsTest(TestCase):
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
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste")

    def _completed_appointment(self, start_time=datetime.time(9, 0)):
        appointment = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=datetime.date.today(), start_time=start_time,
            end_time=datetime.time(start_time.hour + 1, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )
        complete_appointment(appointment=appointment, payment_method="cash", created_by=self.admin)
        return appointment

    def test_revenue_kpi_reflects_completed_appointments(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertContains(response, "R$ 100,00")

    def test_pending_commission_total(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        # 40% de 100 = 40,00 — comissão ainda pendente
        self.assertContains(response, "R$ 40,00")

    def test_low_stock_alert_appears(self):
        create_product(
            tenant=self.tenant, name="Xampu", unit="un",
            cost_price=Decimal("5"), sale_price=Decimal("10"), min_stock_alert=Decimal("5"),
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertContains(response, "estoque baixo")

    def test_no_alerts_shows_calm_state(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertContains(response, "Nada pedindo atenção urgente")

    def test_overdue_subscriber_alert(self):
        Client.objects.create(
            tenant=self.tenant, phone="11900000001", name="Mensalista Vencida",
            is_subscriber=True, subscription_due_date=datetime.date.today() - datetime.timedelta(days=5),
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertContains(response, "vencida")

    def test_credit_liability_sums_all_clients(self):
        add_client_credit(
            self.client_, amount=Decimal("50"), payment_method="pix", created_by=self.admin
        )
        other_client = Client.objects.create(tenant=self.tenant, phone="11900000002", name="Outra")
        add_client_credit(other_client, amount=Decimal("30"), payment_method="cash", created_by=self.admin)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertContains(response, "R$ 80,00")

    def test_top_services_and_employee_commission_charts_have_data(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertContains(response, "Corte")
        self.assertContains(response, "Ana Silva")

    def test_expense_reduces_today_balance(self):
        self._completed_appointment()
        create_expense(
            tenant=self.tenant, amount=Decimal("30"), payment_method="cash",
            description="Material", created_by=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertContains(response, "R$ 70,00")  # 100 entrada - 30 saída = saldo hoje


class DashboardIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")

    def test_low_stock_from_other_tenant_not_counted(self):
        create_product(
            tenant=self.tenant_b, name="Produto B", unit="un",
            cost_price=Decimal("5"), sale_price=Decimal("10"), min_stock_alert=Decimal("100"),
        )
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/dashboard/")
        self.assertContains(response, "Nada pedindo atenção urgente")

    def test_credit_liability_scoped_per_tenant(self):
        client_b = Client.objects.create(tenant=self.tenant_b, phone="11900000003", name="Cliente B")
        add_client_credit(client_b, amount=Decimal("999"), payment_method="pix", created_by=self.admin_b)
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/dashboard/")
        self.assertNotContains(response, "999,00")


class AlpineRegressionTest(TestCase):
    """O dashboard não tem x-data próprio (só Chart.js), mas confirma que a
    página renderiza sem erro de template com o script de gráficos embutido."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_chart_json_is_valid(self):
        import json

        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        body = response.content.decode()
        for var in ("revenueData", "statusData", "servicesData", "employeesData"):
            start = body.index(f"const {var} = ") + len(f"const {var} = ")
            end = body.index(";", start)
            json.loads(body[start:end])  # não deve levantar exceção
