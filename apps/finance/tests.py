import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.clients.models import Client
from apps.employees.services import create_employee, link_service
from apps.scheduling.models import Appointment, AppointmentStatus
from apps.services.services import create_service
from apps.tenants.models import Tenant

from . import services as finance_ops
from .models import (
    CashCategory,
    CashFlowType,
    CashTransaction,
    ComandaProductItem,
    Commission,
    CommissionStatus,
    ExpenseCategory,
)

User = get_user_model()


def make_tenant_with_admin(slug):
    tenant = Tenant.objects.create(name=f"Salão {slug}", slug=slug)
    admin = User.objects.create_user(
        email=f"admin@{slug}.com", password="x", role=User.Role.TENANT_ADMIN, tenant=tenant
    )
    return tenant, admin


def make_appointment(tenant, admin, price=Decimal("100.00"), status=AppointmentStatus.CONFIRMED):
    employee = create_employee(
        tenant=tenant, full_name="Ana Silva", email=f"ana-{tenant.slug}@t.com", password="Senha@123",
        default_commission_type="percentage", default_commission_value=Decimal("40.00"),
    )
    service = create_service(
        tenant=tenant, name="Corte", duration_minutes=60, price=price
    )
    client_ = Client.objects.create(tenant=tenant, phone="+5511999990000", name="Cliente Teste")
    return Appointment.objects.create(
        tenant=tenant, client=client_, employee=employee, service=service,
        date=datetime.date.today() + datetime.timedelta(days=1),
        start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
        status=status, price_at_booking=price,
    )


class CashTransactionDomainTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_create_cash_transaction(self):
        txn = finance_ops.create_cash_transaction(
            tenant=self.tenant, type=CashFlowType.IN, category=CashCategory.OTHER,
            amount=Decimal("50.00"), payment_method="cash", created_by=self.admin,
        )
        self.assertEqual(txn.tenant, self.tenant)
        self.assertEqual(txn.amount, Decimal("50.00"))

    def test_zero_amount_rejected(self):
        with self.assertRaises(ValidationError):
            finance_ops.create_cash_transaction(
                tenant=self.tenant, type=CashFlowType.IN, category=CashCategory.OTHER,
                amount=Decimal("0"), payment_method="cash", created_by=self.admin,
            )

    def test_client_credit_payment_method_rejected(self):
        """Crédito do cliente nunca vira lançamento de caixa (evita duplicar
        receita já reconhecida na recarga) — ver apps/clients/services.py."""
        with self.assertRaises(ValidationError):
            finance_ops.create_cash_transaction(
                tenant=self.tenant, type=CashFlowType.IN, category=CashCategory.SERVICE_SALE,
                amount=Decimal("50.00"), payment_method="client_credit", created_by=self.admin,
            )

    def test_create_expense(self):
        txn = finance_ops.create_expense(
            tenant=self.tenant, amount=Decimal("300.00"), payment_method="pix",
            description="Aluguel", created_by=self.admin,
        )
        self.assertEqual(txn.type, CashFlowType.OUT)
        self.assertEqual(txn.category, CashCategory.EXPENSE)
        self.assertEqual(txn.description, "Aluguel")


class CommissionCalculationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_percentage_commission_calculation(self):
        appointment = make_appointment(self.tenant, self.admin, price=Decimal("200.00"))
        commission = finance_ops.create_commission_for_appointment(appointment)
        self.assertEqual(commission.commission_type, "percentage")
        self.assertEqual(commission.commission_value, Decimal("40.00"))
        self.assertEqual(commission.base_amount, Decimal("200.00"))
        self.assertEqual(commission.calculated_amount, Decimal("80.00"))
        self.assertEqual(commission.status, CommissionStatus.PENDING)

    def test_fixed_commission_calculation(self):
        from apps.employees.services import link_service

        appointment = make_appointment(self.tenant, self.admin, price=Decimal("200.00"))
        link_service(
            appointment.employee, appointment.service,
            commission_type="fixed", commission_value=Decimal("30.00"),
        )
        commission = finance_ops.create_commission_for_appointment(appointment)
        self.assertEqual(commission.commission_type, "fixed")
        self.assertEqual(commission.calculated_amount, Decimal("30.00"))

    def test_percentage_rounds_half_up(self):
        appointment = make_appointment(self.tenant, self.admin, price=Decimal("33.33"))
        # 40% de 33.33 = 13.332 -> arredonda para 13.33
        commission = finance_ops.create_commission_for_appointment(appointment)
        self.assertEqual(commission.calculated_amount, Decimal("13.33"))


class MarkCommissionPaidTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_mark_paid_generates_cash_out_and_updates_status(self):
        appointment = make_appointment(self.tenant, self.admin)
        commission = finance_ops.create_commission_for_appointment(appointment)
        finance_ops.mark_commission_paid(
            commission, payment_method="pix", created_by=self.admin
        )
        commission.refresh_from_db()
        self.assertEqual(commission.status, CommissionStatus.PAID)
        self.assertIsNotNone(commission.paid_at)

        cash_txn = CashTransaction.objects.get(related_commission=commission)
        self.assertEqual(cash_txn.type, CashFlowType.OUT)
        self.assertEqual(cash_txn.category, CashCategory.COMMISSION_PAYMENT)
        self.assertEqual(cash_txn.amount, commission.calculated_amount)

    def test_cannot_pay_already_paid_commission(self):
        appointment = make_appointment(self.tenant, self.admin)
        commission = finance_ops.create_commission_for_appointment(appointment)
        finance_ops.mark_commission_paid(commission, payment_method="pix", created_by=self.admin)
        with self.assertRaises(ValidationError):
            finance_ops.mark_commission_paid(commission, payment_method="pix", created_by=self.admin)


class PeriodSummaryTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_balance_and_totals(self):
        finance_ops.create_cash_transaction(
            tenant=self.tenant, type=CashFlowType.IN, category=CashCategory.SERVICE_SALE,
            amount=Decimal("100"), payment_method="cash", created_by=self.admin,
        )
        finance_ops.create_expense(
            tenant=self.tenant, amount=Decimal("30"), payment_method="cash",
            description="Luz", created_by=self.admin,
        )
        today = datetime.date.today()
        summary = finance_ops.period_summary(self.tenant, today, today)
        self.assertEqual(summary["total_in"], Decimal("100"))
        self.assertEqual(summary["total_out"], Decimal("30"))
        self.assertEqual(summary["balance"], Decimal("70"))

    def test_isolated_per_tenant(self):
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        finance_ops.create_expense(
            tenant=other_tenant, amount=Decimal("999"), payment_method="cash",
            description="Não deve aparecer", created_by=other_admin,
        )
        today = datetime.date.today()
        summary = finance_ops.period_summary(self.tenant, today, today)
        self.assertEqual(summary["total_out"], Decimal("0"))


class ExpenseCategoryDomainTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_create_defaults_to_fixed(self):
        category = finance_ops.create_expense_category(tenant=self.tenant, name="Aluguel")
        self.assertTrue(category.is_fixed)
        self.assertTrue(category.is_active)

    def test_create_variable(self):
        category = finance_ops.create_expense_category(
            tenant=self.tenant, name="Taxa de cartão", is_fixed=False
        )
        self.assertFalse(category.is_fixed)

    def test_blank_name_rejected(self):
        with self.assertRaises(ValidationError):
            finance_ops.create_expense_category(tenant=self.tenant, name="   ")

    def test_duplicate_name_per_tenant_rejected_by_db_constraint(self):
        finance_ops.create_expense_category(tenant=self.tenant, name="Aluguel")
        with self.assertRaises(Exception):
            ExpenseCategory.objects.create(tenant=self.tenant, name="Aluguel")

    def test_same_name_allowed_in_different_tenant(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        finance_ops.create_expense_category(tenant=self.tenant, name="Aluguel")
        # não deve levantar
        finance_ops.create_expense_category(tenant=other_tenant, name="Aluguel")

    def test_update_changes_name_and_type(self):
        category = finance_ops.create_expense_category(tenant=self.tenant, name="Aluguel")
        finance_ops.update_expense_category(category, name="Aluguel do salão", is_fixed=False)
        category.refresh_from_db()
        self.assertEqual(category.name, "Aluguel do salão")
        self.assertFalse(category.is_fixed)

    def test_toggle_active(self):
        category = finance_ops.create_expense_category(tenant=self.tenant, name="Aluguel")
        finance_ops.set_expense_category_active(category, False)
        category.refresh_from_db()
        self.assertFalse(category.is_active)


class CreateExpenseWithCategoryTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.category = finance_ops.create_expense_category(tenant=cls.tenant, name="Aluguel")

    def test_expense_saved_with_category(self):
        txn = finance_ops.create_expense(
            tenant=self.tenant, amount=Decimal("2000"), payment_method="pix",
            description="Aluguel de agosto", created_by=self.admin, expense_category=self.category,
        )
        self.assertEqual(txn.expense_category_id, self.category.pk)

    def test_expense_without_category_still_works(self):
        txn = finance_ops.create_expense(
            tenant=self.tenant, amount=Decimal("50"), payment_method="cash",
            description="Sem categoria", created_by=self.admin,
        )
        self.assertIsNone(txn.expense_category_id)

    def test_category_from_other_tenant_rejected(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        other_category = finance_ops.create_expense_category(tenant=other_tenant, name="Aluguel")
        with self.assertRaises(ValidationError):
            finance_ops.create_expense(
                tenant=self.tenant, amount=Decimal("50"), payment_method="cash",
                description="X", created_by=self.admin, expense_category=other_category,
            )


class DreBreakdownTest(TestCase):
    """DRE em cascata (decisão do usuário em 2026-08-06): comissão como
    custo direto, despesa quebrada em fixa/variável pela `ExpenseCategory`."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.fixed_category = finance_ops.create_expense_category(
            tenant=cls.tenant, name="Aluguel", is_fixed=True
        )
        cls.variable_category = finance_ops.create_expense_category(
            tenant=cls.tenant, name="Taxa de cartão", is_fixed=False
        )

    def _today_range(self):
        today = datetime.date.today()
        return today, today

    def test_revenue_from_service_sale(self):
        finance_ops.create_cash_transaction(
            tenant=self.tenant, type=CashFlowType.IN, category=CashCategory.SERVICE_SALE,
            amount=Decimal("100"), payment_method="cash", created_by=self.admin,
        )
        start, end = self._today_range()
        dre = finance_ops.dre_breakdown(self.tenant, start, end)
        self.assertEqual(dre["revenue"], Decimal("100"))

    def test_commission_is_direct_cost_not_expense(self):
        finance_ops.create_cash_transaction(
            tenant=self.tenant, type=CashFlowType.IN, category=CashCategory.SERVICE_SALE,
            amount=Decimal("100"), payment_method="cash", created_by=self.admin,
        )
        finance_ops.create_cash_transaction(
            tenant=self.tenant, type=CashFlowType.OUT, category=CashCategory.COMMISSION_PAYMENT,
            amount=Decimal("40"), payment_method="cash", created_by=self.admin,
        )
        start, end = self._today_range()
        dre = finance_ops.dre_breakdown(self.tenant, start, end)
        self.assertEqual(dre["direct_cost"], Decimal("40"))
        self.assertEqual(dre["contribution_margin"], Decimal("60"))
        # comissão não pode contar como despesa fixa/variável nem "sem categoria"
        self.assertEqual(dre["fixed_total"], Decimal("0"))
        self.assertEqual(dre["variable_total"], Decimal("0"))
        self.assertEqual(dre["uncategorized_total"], Decimal("0"))

    def test_fixed_and_variable_expenses_split_correctly(self):
        finance_ops.create_expense(
            tenant=self.tenant, amount=Decimal("2000"), payment_method="pix",
            description="Aluguel", created_by=self.admin, expense_category=self.fixed_category,
        )
        finance_ops.create_expense(
            tenant=self.tenant, amount=Decimal("50"), payment_method="credit_card",
            description="Taxa", created_by=self.admin, expense_category=self.variable_category,
        )
        start, end = self._today_range()
        dre = finance_ops.dre_breakdown(self.tenant, start, end)
        self.assertEqual(dre["fixed_total"], Decimal("2000"))
        self.assertEqual(dre["variable_total"], Decimal("50"))
        self.assertEqual(dre["fixed_by_category"], [{"name": "Aluguel", "total": Decimal("2000")}])
        self.assertEqual(
            dre["variable_by_category"], [{"name": "Taxa de cartão", "total": Decimal("50")}]
        )

    def test_expense_without_category_is_uncategorized_not_lost(self):
        finance_ops.create_expense(
            tenant=self.tenant, amount=Decimal("30"), payment_method="cash",
            description="Sem categoria", created_by=self.admin,
        )
        start, end = self._today_range()
        dre = finance_ops.dre_breakdown(self.tenant, start, end)
        self.assertEqual(dre["uncategorized_total"], Decimal("30"))
        self.assertEqual(dre["fixed_total"], Decimal("0"))
        self.assertEqual(dre["variable_total"], Decimal("0"))

    def test_result_matches_revenue_minus_all_costs(self):
        finance_ops.create_cash_transaction(
            tenant=self.tenant, type=CashFlowType.IN, category=CashCategory.SERVICE_SALE,
            amount=Decimal("1000"), payment_method="cash", created_by=self.admin,
        )
        finance_ops.create_cash_transaction(
            tenant=self.tenant, type=CashFlowType.OUT, category=CashCategory.COMMISSION_PAYMENT,
            amount=Decimal("400"), payment_method="cash", created_by=self.admin,
        )
        finance_ops.create_expense(
            tenant=self.tenant, amount=Decimal("200"), payment_method="pix",
            description="Aluguel", created_by=self.admin, expense_category=self.fixed_category,
        )
        finance_ops.create_expense(
            tenant=self.tenant, amount=Decimal("30"), payment_method="cash",
            description="Sem categoria", created_by=self.admin,
        )
        start, end = self._today_range()
        dre = finance_ops.dre_breakdown(self.tenant, start, end)
        # 1000 - 400 (comissão) - 200 (fixa) - 30 (sem categoria) = 370
        self.assertEqual(dre["result"], Decimal("370"))

    def test_isolated_per_tenant(self):
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        other_category = finance_ops.create_expense_category(
            tenant=other_tenant, name="Aluguel", is_fixed=True
        )
        finance_ops.create_expense(
            tenant=other_tenant, amount=Decimal("999"), payment_method="cash",
            description="Não deve aparecer", created_by=other_admin, expense_category=other_category,
        )
        start, end = self._today_range()
        dre = finance_ops.dre_breakdown(self.tenant, start, end)
        self.assertEqual(dre["fixed_total"], Decimal("0"))


class ExpenseCategoryPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )

    def test_login_required(self):
        response = self.client.get("/painel/caixa/categorias-despesa/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee_user)
        response = self.client.get("/painel/caixa/categorias-despesa/")
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_category(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/categorias-despesa/nova/", {"name": "Aluguel", "is_fixed": "True"}
        )
        self.assertEqual(response.status_code, 200)
        category = ExpenseCategory.objects.get(tenant=self.tenant, name="Aluguel")
        self.assertTrue(category.is_fixed)

    def test_admin_creates_variable_category(self):
        self.client.force_login(self.admin)
        self.client.post(
            "/painel/caixa/categorias-despesa/nova/", {"name": "Taxa de cartão", "is_fixed": "False"}
        )
        category = ExpenseCategory.objects.get(tenant=self.tenant, name="Taxa de cartão")
        self.assertFalse(category.is_fixed)

    def test_admin_toggles_category(self):
        category = finance_ops.create_expense_category(tenant=self.tenant, name="Aluguel")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/caixa/categorias-despesa/{category.pk}/ativar-desativar/"
        )
        self.assertEqual(response.status_code, 200)
        category.refresh_from_db()
        self.assertFalse(category.is_active)

    def test_isolation_admin_cannot_edit_other_tenant_category(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        other_category = finance_ops.create_expense_category(tenant=other_tenant, name="Aluguel")
        self.client.force_login(self.admin)
        response = self.client.get(
            f"/painel/caixa/categorias-despesa/{other_category.pk}/editar/"
        )
        self.assertEqual(response.status_code, 404)


class ExpenseFormWithCategoryPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.category = finance_ops.create_expense_category(tenant=cls.tenant, name="Energia Elétrica")

    def test_expense_create_with_category(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/despesa/nova/",
            {
                "amount": "2000,00", "payment_method": "pix", "description": "Aluguel",
                "expense_category": self.category.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        txn = CashTransaction.objects.get(tenant=self.tenant, description="Aluguel")
        self.assertEqual(txn.expense_category_id, self.category.pk)

    def test_expense_create_without_category_still_works(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/despesa/nova/",
            {"amount": "50,00", "payment_method": "cash", "description": "Avulsa"},
        )
        self.assertEqual(response.status_code, 200)
        txn = CashTransaction.objects.get(tenant=self.tenant, description="Avulsa")
        self.assertIsNone(txn.expense_category_id)

    def test_inactive_category_not_offered(self):
        finance_ops.set_expense_category_active(self.category, False)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/despesa/nova/")
        self.assertNotContains(response, "Energia Elétrica")


class FinanceAPIPermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )

    def test_anonymous_denied(self):
        response = APIClient().get("/api/v1/cash-transactions/")
        self.assertEqual(response.status_code, 403)

    def test_employee_can_read_but_not_create_expense(self):
        client = APIClient()
        client.force_authenticate(self.employee)
        self.assertEqual(client.get("/api/v1/cash-transactions/").status_code, 200)
        response = client.post(
            "/api/v1/cash-transactions/",
            {"amount": "10", "payment_method": "cash", "description": "x"},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_create_expense_via_api(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post(
            "/api/v1/cash-transactions/",
            {"amount": "10", "payment_method": "cash", "description": "Despesa API"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(CashTransaction.objects.get().category, CashCategory.EXPENSE)

    def test_admin_can_pay_commission_via_api(self):
        appointment = make_appointment(self.tenant, self.admin)
        commission = finance_ops.create_commission_for_appointment(appointment)
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post(
            f"/api/v1/commissions/{commission.pk}/pay/", {"payment_method": "cash"}
        )
        self.assertEqual(response.status_code, 200)
        commission.refresh_from_db()
        self.assertEqual(commission.status, CommissionStatus.PAID)


class FinanceIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")

    def test_api_list_only_own_tenant(self):
        finance_ops.create_expense(
            tenant=self.tenant_a, amount=Decimal("10"), payment_method="cash",
            description="A", created_by=self.admin_a,
        )
        finance_ops.create_expense(
            tenant=self.tenant_b, amount=Decimal("20"), payment_method="cash",
            description="B", created_by=self.admin_b,
        )
        client = APIClient()
        client.force_authenticate(self.admin_a)
        response = client.get("/api/v1/cash-transactions/")
        descriptions = [item["description"] for item in response.json()]
        self.assertEqual(descriptions, ["A"])

    def test_panel_login_required(self):
        response = self.client.get("/painel/caixa/")
        self.assertEqual(response.status_code, 302)


class FinancePanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_employee_forbidden(self):
        employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=self.tenant
        )
        self.client.force_login(employee)
        response = self.client.get("/painel/caixa/")
        self.assertEqual(response.status_code, 403)

    def test_register_expense_via_htmx(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/despesa/nova/",
            {"amount": "150.00", "payment_method": "pix", "description": "Material de limpeza"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            CashTransaction.objects.filter(tenant=self.tenant, description="Material de limpeza").exists()
        )

    def test_register_expense_with_comma_decimal_amount(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/despesa/nova/",
            {"amount": "150,75", "payment_method": "pix", "description": "Conta de luz"},
        )
        self.assertEqual(response.status_code, 200)
        txn = CashTransaction.objects.get(tenant=self.tenant, description="Conta de luz")
        self.assertEqual(txn.amount, Decimal("150.75"))

    def test_pay_commission_via_htmx(self):
        appointment = make_appointment(self.tenant, self.admin)
        commission = finance_ops.create_commission_for_appointment(appointment)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/caixa/comissoes/{commission.pk}/pagar/",
            {"payment_method": "cash"},
        )
        self.assertEqual(response.status_code, 200)
        commission.refresh_from_db()
        self.assertEqual(commission.status, CommissionStatus.PAID)

    def test_pay_commission_confirm_modal_renders(self):
        appointment = make_appointment(self.tenant, self.admin)
        commission = finance_ops.create_commission_for_appointment(appointment)
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/caixa/comissoes/{commission.pk}/pagar/confirmar/")
        self.assertContains(response, "Pagar comissão")
        self.assertContains(response, "Ana Silva")


class MyCommissionsTest(TestCase):
    """RF12 — só as próprias comissões do funcionário, nunca de outro nem o
    caixa geral do tenant."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.ana = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.bia = create_employee(
            tenant=cls.tenant, full_name="Bia Souza", email="bia@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("50.00"),
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.client_ = Client.objects.create(
            tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste"
        )

    def _appointment_for(self, employee, start_time=datetime.time(9, 0), price=Decimal("100.00")):
        return Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=employee, service=self.service,
            date=datetime.date.today(), start_time=start_time,
            end_time=datetime.time(start_time.hour + 1, 0),
            status=AppointmentStatus.CONFIRMED, price_at_booking=price,
        )

    def test_login_required(self):
        response = self.client.get("/painel/minha-comissao/")
        self.assertEqual(response.status_code, 302)

    def test_employee_sees_only_own_commissions(self):
        ana_commission = finance_ops.create_commission_for_appointment(self._appointment_for(self.ana))
        finance_ops.create_commission_for_appointment(self._appointment_for(self.bia))

        self.client.force_login(self.ana.user)
        response = self.client.get("/painel/minha-comissao/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "R$ 40,00")
        # comissão da Bia (50%) não deve aparecer na tela da Ana
        self.assertNotContains(response, "R$ 50,00")

    def test_pending_and_paid_totals(self):
        pending = finance_ops.create_commission_for_appointment(
            self._appointment_for(self.ana, price=Decimal("100.00"))
        )
        paid = finance_ops.create_commission_for_appointment(
            self._appointment_for(self.ana, start_time=datetime.time(11, 0), price=Decimal("200.00"))
        )
        finance_ops.mark_commission_paid(paid, payment_method="cash", created_by=self.admin)

        self.client.force_login(self.ana.user)
        response = self.client.get("/painel/minha-comissao/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "R$ 40,00")
        self.assertContains(response, "R$ 80,00")

    def test_employee_cannot_access_general_cash_panel(self):
        self.client.force_login(self.ana.user)
        response = self.client.get("/painel/caixa/")
        self.assertEqual(response.status_code, 403)


class ComandaFinalizeTest(TestCase):
    """Aba Comandas do Caixa: atendimentos "Em Atendimento" viram card com
    seletor de produtos (preço sempre de `Product.sale_price`) e finalizar
    aqui conclui o atendimento (comissão + caixa + estoque, atômico)."""

    @classmethod
    def setUpTestData(cls):
        from apps.inventory.services import create_product, register_stock_movement

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste")
        cls.shampoo = create_product(
            tenant=cls.tenant, name="Shampoo", unit="un",
            cost_price=Decimal("10.00"), sale_price=Decimal("30.00"), min_stock_alert=Decimal("1"),
        )
        cls.ampola = create_product(
            tenant=cls.tenant, name="Ampola", unit="un",
            cost_price=Decimal("5.00"), sale_price=Decimal("15.00"), min_stock_alert=Decimal("1"),
        )
        for product in (cls.shampoo, cls.ampola):
            register_stock_movement(
                tenant=cls.tenant, product=product, movement_type="in",
                quantity=Decimal("10"), unit_price=Decimal("1.00"), reason="purchase",
                created_by=cls.admin,
            )

    def _make_in_progress_appointment(self):
        return Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=datetime.date.today(), start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )

    def _assert_all_x_data_balanced(self, body):
        """Checa balanceamento de colchetes/chaves em TODO bloco `x-data="..."`
        da página — regressão do bug em que um `cart: [` sem `]` de
        fechamento quebrava o parse do Alpine.js inteiro sem gerar nenhum
        erro visível. O fim do atributo é a próxima aspa dupla crua (o
        filtro `escapejs` nunca deixa passar uma aspa dupla literal no
        conteúdo, então isso vale mesmo quando `x-data` não é o último
        atributo da tag, ex.: `x-data="{...}" class="...">`)."""
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

    def test_caixa_shows_comanda_card_with_sell_button(self):
        self._make_in_progress_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Cliente Teste")
        self.assertContains(response, "Vender produto")

    def test_product_picker_modal_lists_products_grouped_by_category(self):
        from apps.inventory.services import create_category

        category = create_category(tenant=self.tenant, name="Cabelo")
        self.shampoo.category = category
        self.shampoo.save(update_fields=["category"])
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cabelo")
        self.assertContains(response, "Shampoo")
        self.assertContains(response, "Sem categoria")
        self.assertContains(response, "Ampola")

    def test_product_picker_scoped_to_tenant(self):
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        self.client.force_login(other_admin)
        response = self.client.get(f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/")
        self.assertEqual(response.status_code, 404)

    def test_product_picker_x_data_brackets_are_balanced(self):
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/")
        body = response.content.decode()
        found = self._assert_all_x_data_balanced(body)
        self.assertGreaterEqual(found, 2)  # modal raiz + ao menos 1 grupo (sem categoria)

    def test_alpine_x_data_brackets_are_balanced(self):
        """Regressão: um `cart: [` sem o `]` de fechamento quebra o parse do
        objeto Alpine.js inteiro — a checkbox/lista de produtos para de
        reagir a cliques, mas nenhum assertContains de texto pega isso (o
        HTML em si continua "correto"). Checa TODOS os blocos x-data da
        página (o do troca-de-aba e o de cada card de comanda)."""
        self._make_in_progress_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/")
        body = response.content.decode()
        found = self._assert_all_x_data_balanced(body)
        self.assertGreaterEqual(found, 2)  # troca-de-aba + pelo menos 1 card de comanda

    def test_finalizes_with_two_products_at_sale_price(self):
        appointment = self._make_in_progress_appointment()
        self.client.force_login(self.admin)
        # produtos entram na comanda via o carrinho persistente (item pendente)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.shampoo.pk)},
        )
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.ampola.pk)},
        )
        shampoo_item = ComandaProductItem.objects.get(client=self.client_, product=self.shampoo)
        self.client.post(
            f"/painel/caixa/comandas/produtos/{shampoo_item.pk}/atualizar/", {"quantity": "2"}
        )

        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(appointment.pk)],
                "payment_method": "cash",
            },
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)

        shampoo_txn = CashTransaction.objects.get(
            category=CashCategory.PRODUCT_SALE, description__icontains="Shampoo"
        )
        self.assertEqual(shampoo_txn.amount, Decimal("60.00"))  # 2 x 30,00 (sale_price)
        ampola_txn = CashTransaction.objects.get(
            category=CashCategory.PRODUCT_SALE, description__icontains="Ampola"
        )
        self.assertEqual(ampola_txn.amount, Decimal("15.00"))  # 1 x 15,00 (sale_price)

        self.shampoo.refresh_from_db()
        self.ampola.refresh_from_db()
        self.assertEqual(self.shampoo.current_stock, Decimal("8"))
        self.assertEqual(self.ampola.current_stock, Decimal("9"))

        self.assertTrue(Commission.objects.filter(appointment=appointment).exists())
        # itens pendentes viraram venda de verdade — não sobra mais nenhum
        self.assertFalse(ComandaProductItem.objects.filter(client=self.client_).exists())

    def test_adding_same_product_twice_does_not_duplicate_line(self):
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.shampoo.pk)},
        )
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.shampoo.pk)},
        )
        self.assertEqual(
            ComandaProductItem.objects.filter(client=self.client_, product=self.shampoo).count(), 1
        )

    def test_cannot_finalize_appointment_not_in_progress(self):
        appointment = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=datetime.date.today(), start_time=datetime.time(11, 0), end_time=datetime.time(12, 0),
            status=AppointmentStatus.CONFIRMED, price_at_booking=Decimal("100.00"),
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(appointment.pk)],
                "payment_method": "cash",
            },
        )
        self.assertEqual(response.status_code, 409)

    def test_shows_in_progress_appointments_from_previous_days(self):
        """Decisão do usuário em 2026-07-29: comanda aberta é comanda aberta,
        não importa o dia — senão uma comanda esquecida nunca mais aparece
        pra ser fechada ou cancelada. A data some do card só quando é hoje."""
        Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=datetime.date.today() - datetime.timedelta(days=1),
            start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Cliente Teste")

    def test_comanda_shows_client_credit_balance(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("75"), payment_method="pix", created_by=self.admin
        )
        self._make_in_progress_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "R$ 75,00")

    def test_finalize_paying_with_client_credit_via_panel(self):
        from apps.clients.models import ClientCreditTransaction
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("200"), payment_method="pix", created_by=self.admin
        )
        appointment = self._make_in_progress_appointment()
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(appointment.pk)],
                "payment_method": "client_credit",
            },
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)
        self.assertFalse(
            CashTransaction.objects.filter(related_appointment=appointment).exists()
        )
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.credit_balance, Decimal("100.00"))
        self.assertTrue(
            ClientCreditTransaction.objects.filter(related_appointment=appointment).exists()
        )

    def test_finalize_with_partial_credit_amount_via_panel(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("30"), payment_method="pix", created_by=self.admin
        )
        appointment = self._make_in_progress_appointment()  # serviço R$100
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(appointment.pk)],
                "payment_method": "cash",
                "credit_amount": "30",
            },
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)
        cash_txn = CashTransaction.objects.get(related_appointment=appointment)
        self.assertEqual(cash_txn.amount, Decimal("70.00"))
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.credit_balance, Decimal("0.00"))


class ComandaProductItemDomainTest(TestCase):
    """Carrinho de produto persistido no banco (não mais em Alpine.js) — é o
    que garante que o item sobrevive a trocar de aba/página antes de fechar
    a comanda (bug relatado pelo usuário)."""

    @classmethod
    def setUpTestData(cls):
        from apps.inventory.services import create_product

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste")
        cls.shampoo = create_product(
            tenant=cls.tenant, name="Shampoo", unit="un",
            cost_price=Decimal("10.00"), sale_price=Decimal("30.00"), min_stock_alert=Decimal("1"),
        )
        cls.oleo = create_product(
            tenant=cls.tenant, name="Óleo", unit="ml",
            cost_price=Decimal("2.00"), sale_price=Decimal("5.00"), min_stock_alert=Decimal("1"),
        )

    def test_add_creates_pending_row_with_quantity_1(self):
        item = finance_ops.add_comanda_product_item(
            client=self.client_, product=self.shampoo, created_by=self.admin
        )
        self.assertEqual(item.quantity, Decimal("1"))
        self.assertEqual(item.client, self.client_)

    def test_add_same_product_twice_is_idempotent(self):
        finance_ops.add_comanda_product_item(client=self.client_, product=self.shampoo, created_by=self.admin)
        finance_ops.add_comanda_product_item(client=self.client_, product=self.shampoo, created_by=self.admin)
        self.assertEqual(
            ComandaProductItem.objects.filter(client=self.client_, product=self.shampoo).count(), 1
        )

    def test_set_quantity_updates_value(self):
        item = finance_ops.add_comanda_product_item(client=self.client_, product=self.oleo, created_by=self.admin)
        finance_ops.set_comanda_product_item_quantity(item, Decimal("2.5"))
        item.refresh_from_db()
        self.assertEqual(item.quantity, Decimal("2.5"))

    def test_set_quantity_rejects_zero_or_negative(self):
        item = finance_ops.add_comanda_product_item(client=self.client_, product=self.shampoo, created_by=self.admin)
        with self.assertRaises(ValidationError):
            finance_ops.set_comanda_product_item_quantity(item, Decimal("0"))
        with self.assertRaises(ValidationError):
            finance_ops.set_comanda_product_item_quantity(item, Decimal("-1"))

    def test_set_quantity_rejects_fraction_for_whole_unit_product(self):
        item = finance_ops.add_comanda_product_item(client=self.client_, product=self.shampoo, created_by=self.admin)
        with self.assertRaises(ValidationError):
            finance_ops.set_comanda_product_item_quantity(item, Decimal("1.5"))

    def test_set_quantity_accepts_fraction_for_measured_unit(self):
        item = finance_ops.add_comanda_product_item(client=self.client_, product=self.oleo, created_by=self.admin)
        finance_ops.set_comanda_product_item_quantity(item, Decimal("1.5"))
        item.refresh_from_db()
        self.assertEqual(item.quantity, Decimal("1.5"))

    def test_remove_deletes_row(self):
        item = finance_ops.add_comanda_product_item(client=self.client_, product=self.shampoo, created_by=self.admin)
        finance_ops.remove_comanda_product_item(item)
        self.assertFalse(ComandaProductItem.objects.filter(pk=item.pk).exists())

    def test_items_scoped_per_client(self):
        other_client = Client.objects.create(tenant=self.tenant, phone="+5511988880000", name="Outra Cliente")
        finance_ops.add_comanda_product_item(client=self.client_, product=self.shampoo, created_by=self.admin)
        finance_ops.add_comanda_product_item(client=other_client, product=self.oleo, created_by=self.admin)
        self.assertEqual(finance_ops.comanda_product_items_for_client(self.client_).count(), 1)
        self.assertEqual(finance_ops.comanda_product_items_for_client(other_client).count(), 1)


class SellProductsDomainTest(TestCase):
    """Venda avulsa de produto — cliente entra só pra comprar, sem nenhum
    serviço/agendamento envolvido."""

    @classmethod
    def setUpTestData(cls):
        from apps.inventory.services import create_product, register_stock_movement

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.shampoo = create_product(
            tenant=cls.tenant, name="Shampoo", unit="un",
            cost_price=Decimal("10.00"), sale_price=Decimal("30.00"), min_stock_alert=Decimal("1"),
        )
        register_stock_movement(
            tenant=cls.tenant, product=cls.shampoo, movement_type="in",
            quantity=Decimal("10"), unit_price=Decimal("1.00"), reason="purchase",
            created_by=cls.admin,
        )

    def test_creates_stock_movement_and_cash_transaction(self):
        finance_ops.sell_products(
            tenant=self.tenant,
            product_usage=[{"product": self.shampoo, "quantity": Decimal("2"), "unit_price": Decimal("30.00")}],
            payment_method="cash",
            created_by=self.admin,
        )
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.current_stock, Decimal("8"))
        txn = CashTransaction.objects.get(category=CashCategory.PRODUCT_SALE)
        self.assertEqual(txn.amount, Decimal("60.00"))
        self.assertIsNone(txn.related_appointment)

    def test_no_commission_generated(self):
        finance_ops.sell_products(
            tenant=self.tenant,
            product_usage=[{"product": self.shampoo, "quantity": Decimal("1"), "unit_price": Decimal("30.00")}],
            payment_method="cash",
            created_by=self.admin,
        )
        self.assertFalse(Commission.objects.exists())

    def test_empty_product_usage_rejected(self):
        with self.assertRaises(ValidationError):
            finance_ops.sell_products(
                tenant=self.tenant, product_usage=[], payment_method="cash", created_by=self.admin
            )

    def test_insufficient_stock_rolls_back(self):
        cash_before = CashTransaction.objects.count()
        with self.assertRaises(ValidationError):
            finance_ops.sell_products(
                tenant=self.tenant,
                product_usage=[
                    {"product": self.shampoo, "quantity": Decimal("999"), "unit_price": Decimal("30.00")}
                ],
                payment_method="cash",
                created_by=self.admin,
            )
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.current_stock, Decimal("10"))
        self.assertEqual(CashTransaction.objects.count(), cash_before)


class ComandaProductPersistenceAndSalePanelTest(TestCase):
    """Painel: item de comanda sobrevive a uma nova requisição (equivalente a
    trocar de aba/página e voltar), um botão só "Vender produto" por
    comanda, e o fluxo de "Nova Venda" avulsa."""

    @classmethod
    def setUpTestData(cls):
        from apps.inventory.services import create_product, register_stock_movement

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.corte = create_service(tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00"))
        cls.manicure = create_service(tenant=cls.tenant, name="Manicure", duration_minutes=30, price=Decimal("45.00"))
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste")
        cls.shampoo = create_product(
            tenant=cls.tenant, name="Shampoo", unit="un",
            cost_price=Decimal("10.00"), sale_price=Decimal("30.00"), min_stock_alert=Decimal("1"),
        )
        register_stock_movement(
            tenant=cls.tenant, product=cls.shampoo, movement_type="in",
            quantity=Decimal("10"), unit_price=Decimal("1.00"), reason="purchase",
            created_by=cls.admin,
        )

    def _appointment(self, service, start_time=datetime.time(9, 0)):
        return Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=service,
            date=datetime.date.today(), start_time=start_time,
            end_time=datetime.time(start_time.hour + 1, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=service.price,
        )

    def test_product_survives_a_fresh_request_to_caixa(self):
        """O bug relatado: produto adicionado sumia ao trocar de aba/página.
        Uma nova requisição GET (sem nenhum estado de navegador entre elas,
        já que o Django test client não guarda JS) reproduz exatamente isso
        — se o item ainda aparecer, a persistência está correta."""
        self._appointment(self.corte)
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.shampoo.pk)},
        )
        # nova requisição, simulando o usuário saindo e voltando pra página
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Shampoo")

    def test_single_vender_produto_button_even_with_two_services(self):
        self._appointment(self.corte, datetime.time(9, 0))
        self._appointment(self.manicure, datetime.time(10, 0))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/")
        self.assertEqual(response.content.decode().count("Vender produto"), 1)

    def test_update_quantity_via_panel(self):
        self._appointment(self.corte)
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.shampoo.pk)},
        )
        item = ComandaProductItem.objects.get(client=self.client_, product=self.shampoo)
        response = self.client.post(
            f"/painel/caixa/comandas/produtos/{item.pk}/atualizar/", {"quantity": "3"}
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.quantity, Decimal("3"))

    def test_remove_item_via_panel(self):
        self._appointment(self.corte)
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.shampoo.pk)},
        )
        item = ComandaProductItem.objects.get(client=self.client_, product=self.shampoo)
        response = self.client.post(f"/painel/caixa/comandas/produtos/{item.pk}/remover/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ComandaProductItem.objects.filter(pk=item.pk).exists())

    def test_item_update_scoped_to_tenant(self):
        self._appointment(self.corte)
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.shampoo.pk)},
        )
        item = ComandaProductItem.objects.get(client=self.client_, product=self.shampoo)
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        self.client.force_login(other_admin)
        response = self.client.post(
            f"/painel/caixa/comandas/produtos/{item.pk}/atualizar/", {"quantity": "5"}
        )
        self.assertEqual(response.status_code, 404)

    def test_sale_picker_lists_products(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/vendas/nova/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shampoo")

    def test_sale_create_walk_in_purchase_no_appointment(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/vendas/confirmar/",
            {
                "payment_method": "cash",
                "product_id": [str(self.shampoo.pk)],
                "product_qty": ["2"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.current_stock, Decimal("8"))
        txn = CashTransaction.objects.get(category=CashCategory.PRODUCT_SALE)
        self.assertEqual(txn.amount, Decimal("60.00"))
        self.assertIsNone(txn.related_appointment)
        self.assertFalse(Appointment.objects.exists())
        self.assertFalse(Commission.objects.exists())

    def test_sale_create_rejects_empty_cart(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/vendas/confirmar/", {"payment_method": "cash"}
        )
        self.assertEqual(response.status_code, 409)

    def test_employee_forbidden_from_sale_picker(self):
        employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=self.tenant,
        )
        self.client.force_login(employee_user)
        response = self.client.get("/painel/caixa/vendas/nova/")
        self.assertEqual(response.status_code, 403)


class RemoveServiceFromComandaPanelTest(TestCase):
    """Botão de excluir serviço da comanda — pedido do usuário: "se eu
    errar, não consigo mais remover". Também cobre o caso em que remover o
    único serviço deixa produtos pendentes "órfãos" (sem nenhum atendimento
    na comanda) — o carrinho não pode ficar preso sem UI."""

    @classmethod
    def setUpTestData(cls):
        from apps.inventory.services import create_product, register_stock_movement

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste")
        cls.shampoo = create_product(
            tenant=cls.tenant, name="Shampoo", unit="un",
            cost_price=Decimal("10.00"), sale_price=Decimal("30.00"), min_stock_alert=Decimal("1"),
        )
        register_stock_movement(
            tenant=cls.tenant, product=cls.shampoo, movement_type="in",
            quantity=Decimal("10"), unit_price=Decimal("1.00"), reason="purchase",
            created_by=cls.admin,
        )

    def _in_progress_appointment(self, start_time=datetime.time(9, 0)):
        return Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=datetime.date.today(), start_time=start_time,
            end_time=datetime.time(start_time.hour + 1, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )

    def test_confirm_modal_renders(self):
        appointment = self._in_progress_appointment()
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/caixa/comandas/{appointment.pk}/remover/confirmar/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Corte")
        self.assertContains(response, "Cliente Teste")

    def test_remove_via_panel_cancels_appointment(self):
        appointment = self._in_progress_appointment()
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/caixa/comandas/{appointment.pk}/remover/")
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.CANCELED)
        self.assertNotContains(response, "Corte")

    def test_remove_scoped_to_tenant(self):
        appointment = self._in_progress_appointment()
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        self.client.force_login(other_admin)
        response = self.client.post(f"/painel/caixa/comandas/{appointment.pk}/remover/")
        self.assertEqual(response.status_code, 404)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.IN_PROGRESS)

    def test_cannot_remove_twice(self):
        appointment = self._in_progress_appointment()
        self.client.force_login(self.admin)
        self.client.post(f"/painel/caixa/comandas/{appointment.pk}/remover/")
        response = self.client.post(f"/painel/caixa/comandas/{appointment.pk}/remover/")
        self.assertEqual(response.status_code, 404)  # já não está mais em atendimento

    def test_removing_only_service_keeps_pending_products_visible(self):
        appointment = self._in_progress_appointment()
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.shampoo.pk)},
        )
        self.client.post(f"/painel/caixa/comandas/{appointment.pk}/remover/")
        # a comanda some da lista de atendimentos mas o produto pendente
        # continua visível (não fica "preso" sem nenhuma UI)
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Cliente Teste")
        self.assertContains(response, "Shampoo")

    def test_finalizing_product_only_comanda_after_removing_service(self):
        appointment = self._in_progress_appointment()
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.shampoo.pk)},
        )
        self.client.post(f"/painel/caixa/comandas/{appointment.pk}/remover/")

        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {"client_id": str(self.client_.pk), "payment_method": "cash"},
        )
        self.assertEqual(response.status_code, 200)
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.current_stock, Decimal("9"))
        txn = CashTransaction.objects.get(category=CashCategory.PRODUCT_SALE)
        self.assertEqual(txn.amount, Decimal("30.00"))
        self.assertIsNone(txn.related_appointment)
        self.assertFalse(Commission.objects.exists())
        self.assertFalse(ComandaProductItem.objects.filter(client=self.client_).exists())
        # venda avulsa de produto (sem serviço) não tem contexto de
        # atendimento — não faz sentido perguntar sobre preferências aqui.
        self.assertNotContains(response, "Atendimento finalizado!")

    def test_removing_only_service_with_no_products_disappears_from_list(self):
        appointment = self._in_progress_appointment()
        self.client.force_login(self.admin)
        self.client.post(f"/painel/caixa/comandas/{appointment.pk}/remover/")
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Nenhuma comanda em aberto.")


class WalkInServiceAndGroupFinalizeTest(TestCase):
    """Cliente decide fazer um serviço extra na hora — o novo atendimento
    entra na MESMA comanda (agrupada por cliente) e a comanda inteira fecha
    com um pagamento só, mesmo com profissionais diferentes."""

    @classmethod
    def setUpTestData(cls):
        from apps.inventory.services import create_product, register_stock_movement

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.ana = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.julia = create_employee(
            tenant=cls.tenant, full_name="Júlia Mendes", email="julia@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.corte = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.manicure = create_service(
            tenant=cls.tenant, name="Manicure", duration_minutes=30, price=Decimal("45.00")
        )
        link_service(cls.ana, cls.corte)
        link_service(cls.julia, cls.manicure)
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste")
        cls.esmalte = create_product(
            tenant=cls.tenant, name="Esmalte", unit="un",
            cost_price=Decimal("5.00"), sale_price=Decimal("20.00"), min_stock_alert=Decimal("1"),
        )
        register_stock_movement(
            tenant=cls.tenant, product=cls.esmalte, movement_type="in",
            quantity=Decimal("10"), unit_price=Decimal("1.00"), reason="purchase",
            created_by=cls.admin,
        )

    def _corte_appointment(self):
        return Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.ana, service=self.corte,
            date=datetime.date.today(), start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )

    def test_service_picker_lists_bookable_services(self):
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/caixa/comandas/cliente/{self.client_.pk}/servico/")
        self.assertContains(response, "Corte")
        self.assertContains(response, "Manicure")

    def test_employee_list_shows_only_linked_active_employees(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/servico/{self.corte.pk}/profissionais/"
        )
        self.assertContains(response, "Ana Silva")
        self.assertNotContains(response, "Júlia Mendes")

    def test_add_walk_in_service_creates_in_progress_appointment(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/servico/adicionar/",
            {"service_id": self.manicure.pk, "employee_id": self.julia.pk},
        )
        self.assertEqual(response.status_code, 200)
        appointment = Appointment.objects.get(client=self.client_, service=self.manicure)
        self.assertEqual(appointment.status, AppointmentStatus.IN_PROGRESS)
        self.assertEqual(appointment.employee, self.julia)

    def test_comanda_groups_two_services_under_same_client(self):
        self._corte_appointment()
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/servico/adicionar/",
            {"service_id": self.manicure.pk, "employee_id": self.julia.pk},
        )
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Corte")
        self.assertContains(response, "Manicure")
        self.assertContains(response, "Ana Silva")
        self.assertContains(response, "Júlia Mendes")
        # o nome do cliente aparece 1 vez só (agrupado), não 1 por serviço
        self.assertEqual(response.content.decode().count("Cliente Teste"), 1)

    def test_finalize_group_completes_both_with_one_payment(self):
        corte_appt = self._corte_appointment()
        manicure_appt = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.julia, service=self.manicure,
            date=datetime.date.today(), start_time=datetime.time(10, 0), end_time=datetime.time(10, 30),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("45.00"),
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(corte_appt.pk), str(manicure_appt.pk)],
                "payment_method": "cash",
            },
        )
        self.assertEqual(response.status_code, 200)
        corte_appt.refresh_from_db()
        manicure_appt.refresh_from_db()
        self.assertEqual(corte_appt.status, AppointmentStatus.COMPLETED)
        self.assertEqual(manicure_appt.status, AppointmentStatus.COMPLETED)
        self.assertTrue(Commission.objects.filter(appointment=corte_appt, employee=self.ana).exists())
        self.assertTrue(Commission.objects.filter(appointment=manicure_appt, employee=self.julia).exists())

    def test_finalize_group_prompts_to_update_client_preferences(self):
        """Depois de finalizar um atendimento de verdade, o Caixa pergunta se
        o admin quer atualizar as observações/preferências do cliente."""
        corte_appt = self._corte_appointment()
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(corte_appt.pk)],
                "payment_method": "cash",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Atendimento finalizado!")
        self.assertContains(response, "Cliente Teste")
        self.assertContains(response, f"/painel/clientes/{self.client_.pk}/preferencias/editar/")

    def test_finalize_group_with_product_added_via_persistent_cart(self):
        """Um botão só de "Vender produto" pra comanda inteira (não mais por
        serviço) — o produto entra pelo carrinho persistente do cliente."""
        corte_appt = self._corte_appointment()
        manicure_appt = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.julia, service=self.manicure,
            date=datetime.date.today(), start_time=datetime.time(10, 0), end_time=datetime.time(10, 30),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("45.00"),
        )
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/produtos/adicionar/",
            {"product_id": str(self.esmalte.pk)},
        )
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(corte_appt.pk), str(manicure_appt.pk)],
                "payment_method": "cash",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            CashTransaction.objects.filter(category=CashCategory.PRODUCT_SALE, description__icontains="Esmalte").exists()
        )
        self.assertFalse(
            ComandaProductItem.objects.filter(client=self.client_).exists()
        )

    def test_finalize_group_with_partial_credit_and_remainder_in_cash(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("120"), payment_method="pix", created_by=self.admin
        )
        corte_appt = self._corte_appointment()  # 100
        manicure_appt = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.julia, service=self.manicure,
            date=datetime.date.today(), start_time=datetime.time(10, 0), end_time=datetime.time(10, 30),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("45.00"),
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(corte_appt.pk), str(manicure_appt.pk)],
                "payment_method": "cash",
                "credit_amount": "120",
            },
        )
        self.assertEqual(response.status_code, 200)
        corte_appt.refresh_from_db()
        manicure_appt.refresh_from_db()
        self.assertEqual(corte_appt.status, AppointmentStatus.COMPLETED)
        self.assertEqual(manicure_appt.status, AppointmentStatus.COMPLETED)
        # corte (100) coberto inteiro por crédito
        self.assertFalse(CashTransaction.objects.filter(related_appointment=corte_appt).exists())
        # manicure (45): sobrou 20 de crédito, resto (25) em dinheiro
        manicure_txn = CashTransaction.objects.get(related_appointment=manicure_appt)
        self.assertEqual(manicure_txn.amount, Decimal("25.00"))
        self.client_.refresh_from_db()
        self.assertEqual(self.client_.credit_balance, Decimal("0.00"))

    def test_finalize_group_credit_amount_over_total_rejected(self):
        from apps.clients.services import add_client_credit

        add_client_credit(
            self.client_, amount=Decimal("500"), payment_method="pix", created_by=self.admin
        )
        corte_appt = self._corte_appointment()  # 100
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(corte_appt.pk)],
                "payment_method": "cash",
                "credit_amount": "150",
            },
        )
        self.assertEqual(response.status_code, 409)
        corte_appt.refresh_from_db()
        self.assertEqual(corte_appt.status, AppointmentStatus.IN_PROGRESS)

    def test_payment_method_dropdown_no_longer_offers_client_credit(self):
        self._corte_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, 'name="payment_method"')
        self.assertNotContains(response, '>Crédito do cliente<')

    def test_finalize_group_scoped_to_tenant(self):
        corte_appt = self._corte_appointment()
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        self.client.force_login(other_admin)
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": [str(corte_appt.pk)],
                "payment_method": "cash",
            },
        )
        # o cliente é de OUTRO tenant — get_object_or_404 rejeita antes de
        # sequer checar os agendamentos.
        self.assertEqual(response.status_code, 404)
        corte_appt.refresh_from_db()
        self.assertEqual(corte_appt.status, AppointmentStatus.IN_PROGRESS)

    def test_walk_in_add_scoped_to_tenant(self):
        other_tenant, other_admin = make_tenant_with_admin("salao-b")
        self.client.force_login(other_admin)
        response = self.client.post(
            f"/painel/caixa/comandas/cliente/{self.client_.pk}/servico/adicionar/",
            {"service_id": self.manicure.pk, "employee_id": self.julia.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_employee_forbidden(self):
        employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=self.tenant,
        )
        self.client.force_login(employee_user)
        response = self.client.get(f"/painel/caixa/comandas/cliente/{self.client_.pk}/servico/")
        self.assertEqual(response.status_code, 403)


class CommissionsByEmployeeTabTest(TestCase):
    """Aba Comissões: agrupadas por funcionário, com pagamento individual ou
    em lote ("pagar tudo"), filtrável pelo mesmo período do Caixa."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.ana = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste")

    def _commission_for(self, price=Decimal("100.00"), start_time=datetime.time(9, 0)):
        appointment = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.ana, service=self.service,
            date=datetime.date.today(), start_time=start_time,
            end_time=datetime.time(start_time.hour + 1, 0),
            status=AppointmentStatus.CONFIRMED, price_at_booking=price,
        )
        return finance_ops.create_commission_for_appointment(appointment)

    def test_commissions_grouped_by_employee_with_pay_all_button(self):
        self._commission_for()
        self._commission_for(start_time=datetime.time(11, 0))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Ana Silva")
        self.assertContains(response, "Pagar tudo")

    def test_paid_commission_shows_badge_not_button(self):
        commission = self._commission_for()
        finance_ops.mark_commission_paid(commission, payment_method="cash", created_by=self.admin)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Paga")

    def test_pay_all_confirm_modal_shows_total(self):
        self._commission_for(price=Decimal("100.00"))
        self._commission_for(price=Decimal("200.00"), start_time=datetime.time(11, 0))
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/caixa/comissoes/{self.ana.pk}/pagar-tudo/confirmar/")
        # 40% de 100 + 40% de 200 = 40 + 80 = 120
        self.assertContains(response, "120")

    def test_pay_all_marks_every_pending_commission_paid(self):
        c1 = self._commission_for(price=Decimal("100.00"))
        c2 = self._commission_for(price=Decimal("200.00"), start_time=datetime.time(11, 0))
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/caixa/comissoes/{self.ana.pk}/pagar-tudo/", {"payment_method": "pix"}
        )
        self.assertEqual(response.status_code, 200)
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(c1.status, CommissionStatus.PAID)
        self.assertEqual(c2.status, CommissionStatus.PAID)
        self.assertEqual(
            CashTransaction.objects.filter(category=CashCategory.COMMISSION_PAYMENT).count(), 2
        )

    def test_pay_all_rejected_when_nothing_pending(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/caixa/comissoes/{self.ana.pk}/pagar-tudo/", {"payment_method": "pix"}
        )
        self.assertEqual(response.status_code, 409)

    def test_pay_all_respects_date_filter(self):
        """Comissão fora do período filtrado não deve ser paga pelo "pagar tudo".

        O período filtra por `Commission.created_at` (quando a comissão foi
        gerada), não pela data agendada do atendimento — mesmo critério do
        resto do Caixa (`CashTransaction.created_at`). Simula "fora do
        período" com um `created_at` antigo via `.update()` (contorna
        `auto_now_add`, que sempre grava a hora atual na criação)."""
        in_range = self._commission_for(price=Decimal("100.00"))
        out_of_range = self._commission_for(price=Decimal("200.00"), start_time=datetime.time(11, 0))
        Commission.objects.filter(pk=out_of_range.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=60)
        )

        self.client.force_login(self.admin)
        today = datetime.date.today()
        response = self.client.post(
            f"/painel/caixa/comissoes/{self.ana.pk}/pagar-tudo/?start={today.isoformat()}&end={today.isoformat()}",
            {"payment_method": "pix"},
        )
        self.assertEqual(response.status_code, 200)
        in_range.refresh_from_db()
        out_of_range.refresh_from_db()
        self.assertEqual(in_range.status, CommissionStatus.PAID)
        self.assertEqual(out_of_range.status, CommissionStatus.PENDING)

    def test_commission_of_appointment_completed_ahead_of_scheduled_date_still_shows(self):
        """Regressão: cliente chega adiantado, salão atende e finaliza no
        Caixa antes da data agendada do atendimento (ex.: agendado pra daqui
        a 2 dias, mas feito hoje). A comissão precisa aparecer na aba
        Comissões dentro do período padrão (mês corrente até hoje) — antes
        do fix ela sumia porque o filtro usava `appointment__date` (no
        futuro), enquanto o saldo do Caixa já usava `CashTransaction.created_at`
        (hoje) e mostrava o valor certo — inconsistência que escondia a
        comissão mesmo com o dinheiro batendo."""
        from apps.scheduling.services import complete_appointment

        appointment = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.ana, service=self.service,
            date=datetime.date.today() + datetime.timedelta(days=2),
            start_time=datetime.time(9, 0), end_time=datetime.time(10, 0),
            status=AppointmentStatus.IN_PROGRESS, price_at_booking=Decimal("100.00"),
        )
        complete_appointment(appointment=appointment, payment_method="cash", created_by=self.admin)
        self.client.force_login(self.admin)

        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Ana Silva")
        self.assertContains(response, "Pagar tudo")

        response = self.client.post(
            f"/painel/caixa/comissoes/{self.ana.pk}/pagar-tudo/", {"payment_method": "pix"}
        )
        self.assertEqual(response.status_code, 200)
        commission = Commission.objects.get(appointment=appointment)
        self.assertEqual(commission.status, CommissionStatus.PAID)


class PackageCoveredComandaPanelTest(TestCase):
    """Serviço coberto por pacote de mensalidade aparece marcado "Mensalidade"
    no Caixa, com valor "Incluso" em vez do preço — e o total da comanda não
    conta esse serviço (decisão do usuário em 2026-08-04)."""

    @classmethod
    def setUpTestData(cls):
        from apps.clients.services import assign_package_to_client, create_package

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.ana = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.corte = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        link_service(cls.ana, cls.corte)
        cls.client_ = Client.objects.create(tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste")
        cls.package = create_package(
            tenant=cls.tenant, name="Cabelo Ilimitado", price=Decimal("150.00"),
            service_ids=[cls.corte.pk], generates_commission=True, created_by=cls.admin,
        )
        assign_package_to_client(
            cls.client_, package=cls.package, payment_method="pix", created_by=cls.admin,
        )
        cls.client_.refresh_from_db()

    def _covered_appointment(self):
        from apps.scheduling.services import start_walk_in_service

        return start_walk_in_service(
            tenant=self.tenant, client=self.client_, employee=self.ana,
            service=self.corte, created_by=self.admin,
        )

    def test_caixa_shows_mensalidade_badge_and_incluso(self):
        self._covered_appointment()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/caixa/")
        self.assertContains(response, "Mensalidade")
        self.assertContains(response, "Incluso")

    def test_finalize_charges_only_zero_for_covered_service(self):
        appointment = self._covered_appointment()
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/caixa/comandas/finalizar-grupo/",
            {
                "client_id": str(self.client_.pk),
                "appointment_id": str(appointment.pk),
                "payment_method": "cash",
            },
        )
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, AppointmentStatus.COMPLETED)
        self.assertFalse(
            CashTransaction.objects.filter(
                tenant=self.tenant, category=CashCategory.SERVICE_SALE, related_appointment=appointment,
            ).exists()
        )
        self.assertTrue(Commission.objects.filter(appointment=appointment).exists())
