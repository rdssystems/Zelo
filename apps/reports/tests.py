import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.clients.models import Client
from apps.clients.services import add_client_credit
from apps.employees.services import create_employee
from apps.finance.services import create_expense, sell_products
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
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, "/painel/relatorios/")

    def test_old_dashboard_url_redirects_to_reports(self):
        """Dashboard foi incorporado em Relatórios (aba "Visão Geral") em
        2026-08-05 — link/bookmark antigo não pode quebrar."""
        self.client.force_login(self.admin)
        response = self.client.get("/painel/dashboard/")
        self.assertRedirects(response, "/painel/relatorios/")


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
        self.assertContains(response, "R$ 100,00")  # entrada do DRE / faturamento do mês
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

    def test_period_outside_range_excludes_data_from_faturamento(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        last_year = self.today.replace(year=self.today.year - 1)
        response = self.client.get(
            f"/painel/relatorios/?start={last_year.isoformat()}&end={last_year.isoformat()}"
        )
        # "Corte" só aparece no ranking de serviços da aba Faturamento (filtrado
        # pelo período) — não usa "R$ 100,00" aqui porque a Visão Geral mostra
        # o faturamento do MÊS ATUAL sempre, independente do filtro de período.
        self.assertNotContains(response, "Corte")

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


class VisaoGeralMetricsTest(TestCase):
    """Aba "Visão Geral" — herdada de `apps.dashboard` (incorporada em
    2026-08-05). Mesmos casos de teste que existiam lá, só que contra
    `/painel/relatorios/` em vez de `/painel/dashboard/`."""

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
        response = self.client.get("/painel/relatorios/")
        self.assertContains(response, "R$ 100,00")

    def test_pending_commission_total(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        # 40% de 100 = 40,00 — comissão ainda pendente
        self.assertContains(response, "R$ 40,00")

    def test_low_stock_alert_appears(self):
        create_product(
            tenant=self.tenant, name="Xampu", unit="un",
            cost_price=Decimal("5"), sale_price=Decimal("10"), min_stock_alert=Decimal("5"),
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        self.assertContains(response, "estoque baixo")

    def test_no_alerts_shows_calm_state(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        self.assertContains(response, "Nada pedindo atenção urgente")

    def test_overdue_subscriber_alert(self):
        Client.objects.create(
            tenant=self.tenant, phone="11900000001", name="Mensalista Vencida",
            is_subscriber=True, subscription_due_date=datetime.date.today() - datetime.timedelta(days=5),
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        self.assertContains(response, "vencida")

    def test_credit_liability_sums_all_clients(self):
        add_client_credit(
            self.client_, amount=Decimal("50"), payment_method="pix", created_by=self.admin
        )
        other_client = Client.objects.create(tenant=self.tenant, phone="11900000002", name="Outra")
        add_client_credit(other_client, amount=Decimal("30"), payment_method="cash", created_by=self.admin)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        self.assertContains(response, "R$ 80,00")

    def test_commission_chart_has_employee_data(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        self.assertContains(response, "Ana Silva")

    def test_expense_reduces_today_balance(self):
        self._completed_appointment()
        create_expense(
            tenant=self.tenant, amount=Decimal("30"), payment_method="cash",
            description="Material", created_by=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        self.assertContains(response, "R$ 70,00")  # 100 entrada - 30 saída = saldo hoje


class VisaoGeralIsolationTest(TestCase):
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
        response = self.client.get("/painel/relatorios/")
        self.assertContains(response, "Nada pedindo atenção urgente")

    def test_credit_liability_scoped_per_tenant(self):
        client_b = Client.objects.create(tenant=self.tenant_b, phone="11900000003", name="Cliente B")
        add_client_credit(client_b, amount=Decimal("999"), payment_method="pix", created_by=self.admin_b)
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/relatorios/")
        self.assertNotContains(response, "999,00")


class ChartJsonRegressionTest(TestCase):
    """Confirma que os 6 gráficos (2 da Visão Geral + 4 do Faturamento)
    renderizam JSON válido embutido no template, mesmo com o HTML inteiro
    (as 3 abas) sempre presente na resposta (só o CSS/`x-show` esconde a
    aba inativa)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_chart_json_is_valid(self):
        import json

        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/")
        body = response.content.decode()
        for var in (
            "statusData", "commissionData", "revenueData",
            "servicesData", "productsData", "employeesData",
        ):
            start = body.index(f"const {var} = ") + len(f"const {var} = ")
            end = body.index(";", start)
            json.loads(body[start:end])  # não deve levantar exceção


class ReportsPdfTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )
        cls.employee = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte Feminino", duration_minutes=60, price=Decimal("100.00")
        )
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="11999990000", name="Cliente Teste")
        cls.today = datetime.date.today()

    def _completed_appointment(self, tenant=None, employee=None, service=None, client=None, admin=None):
        appointment = Appointment.objects.create(
            tenant=tenant or self.tenant, client=client or self.client_,
            employee=employee or self.employee, service=service or self.service,
            date=self.today, start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )
        complete_appointment(appointment=appointment, payment_method="cash", created_by=admin or self.admin)
        return appointment

    def _post(self, sections):
        data = {"start": self.today.replace(day=1).isoformat(), "end": self.today.isoformat()}
        if sections is not None:
            data["sections"] = sections
        return self.client.post("/painel/relatorios/pdf/", data)

    def test_login_required(self):
        response = self._post(["visao_geral"])
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee_user)
        response = self._post(["visao_geral"])
        self.assertEqual(response.status_code, 403)

    def test_get_not_allowed(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/relatorios/pdf/")
        self.assertEqual(response.status_code, 405)

    def test_generates_valid_pdf_with_all_sections(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        response = self._post(["visao_geral", "faturamento", "dre"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))
        # ReportLab escapa caractere acentuado como octal (\343) na string do
        # PDF em vez do byte cru — não dá pra procurar "Salão" direto; o nome
        # do serviço (sem acento) já prova que o conteúdo do tenant certo saiu.
        self.assertIn(b"Corte Feminino", response.content)

    def test_header_and_footer_present(self):
        self.client.force_login(self.admin)
        response = self._post(["visao_geral"])
        self.assertIn(b"Zellup", response.content)
        self.assertIn(
            b"Arquivo gerado automaticamente pela plataforma Zellup", response.content
        )

    def test_generates_pdf_with_tenant_logo(self):
        """Exercita o desenho do logo do tenant no cabeçalho (não só o
        caminho sem logo, que é o que os outros testes cobrem)."""
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (300, 300), (10, 20, 30)).save(buffer, format="JPEG")
        buffer.seek(0)
        self.tenant.logo = SimpleUploadedFile("logo.jpg", buffer.read(), content_type="image/jpeg")
        self.tenant.save()

        self.client.force_login(self.admin)
        response = self._post(["visao_geral"])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_empty_sections_falls_back_to_all(self):
        """Botão "Gerar PDF" fica desabilitado sem nenhuma seção marcada no
        modal, mas o servidor não deve depender só do JS pra essa regra."""
        self.client.force_login(self.admin)
        response = self._post(sections=None)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_only_selected_sections_are_included(self):
        self._completed_appointment()
        self.client.force_login(self.admin)
        response = self._post(["dre"])
        self.assertNotIn(b"Atendimentos hoje", response.content)  # só existe na Visão Geral
        self.assertIn(b"Entradas", response.content)  # rótulo do DRE

    def test_isolation_other_tenant_service_not_in_pdf(self):
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        other_employee = create_employee(
            tenant=other_tenant, full_name="Beatriz", email="bia@salao-b.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("30"),
        )
        other_service = create_service(
            tenant=other_tenant, name="ServicoExclusivoB", duration_minutes=30, price=Decimal("50")
        )
        other_client = Client.objects.create(tenant=other_tenant, phone="11988887777", name="Cliente B")
        self._completed_appointment(
            tenant=other_tenant, employee=other_employee, service=other_service,
            client=other_client, admin=other_admin,
        )

        self.client.force_login(self.admin)
        response = self._post(["visao_geral", "faturamento", "dre"])
        self.assertNotIn(b"ServicoExclusivoB", response.content)
