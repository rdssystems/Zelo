import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.clients.models import Client
from apps.finance import services as finance_ops
from apps.scheduling.models import Appointment, AppointmentStatus
from apps.services.services import create_service
from apps.tenants.models import Tenant

from . import services as employee_ops
from .models import CommissionType, Employee, WorkingHours

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


def make_employee(tenant, email="ana@salao.com", **overrides):
    defaults = {
        "full_name": "Ana Silva",
        "password": "Senha@123",
        "default_commission_type": CommissionType.PERCENTAGE,
        "default_commission_value": Decimal("40.00"),
    }
    defaults.update(overrides)
    return employee_ops.create_employee(tenant=tenant, email=email, **defaults)


class CreateEmployeeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_creates_user_automatically(self):
        """RF08: cadastrar funcionário cria o User de login automaticamente."""
        employee = make_employee(self.tenant)
        self.assertEqual(employee.user.role, User.Role.EMPLOYEE)
        self.assertEqual(employee.user.tenant, self.tenant)
        self.assertEqual(employee.user.email, "ana@salao.com")
        self.assertTrue(employee.user.is_active)
        self.assertTrue(employee.user.has_usable_password())

    def test_welcome_email_sent_without_password(self):
        make_employee(self.tenant)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ana@salao.com", mail.outbox[0].body)
        self.assertNotIn("Senha@123", mail.outbox[0].body)
        self.assertNotIn("Senha:", mail.outbox[0].body)

    def test_weak_password_rejected(self):
        with self.assertRaises(ValidationError):
            make_employee(self.tenant, password="123")
        self.assertEqual(Employee.objects.count(), 0)

    def test_duplicate_email_rejected_and_nothing_created(self):
        make_employee(self.tenant)
        with self.assertRaises(ValidationError):
            make_employee(self.tenant, email="ana@salao.com", full_name="Outra")
        self.assertEqual(Employee.objects.count(), 1)
        self.assertEqual(
            User.objects.filter(email="ana@salao.com").count(), 1
        )

    def test_percentage_above_100_rejected(self):
        with self.assertRaises(ValidationError):
            make_employee(
                self.tenant, default_commission_value=Decimal("150.00")
            )

    def test_deactivate_blocks_login(self):
        employee = make_employee(self.tenant)
        employee_ops.set_employee_active(employee, False)
        employee.user.refresh_from_db()
        self.assertFalse(employee.user.is_active)

    def test_delete_active_employee_rejected(self):
        employee = make_employee(self.tenant)
        with self.assertRaises(ValidationError):
            employee_ops.delete_employee(employee)
        self.assertTrue(Employee.objects.filter(pk=employee.pk).exists())

    def test_delete_inactive_employee_removes_employee_and_login(self):
        employee = make_employee(self.tenant)
        user_id = employee.user_id
        employee_ops.set_employee_active(employee, False)
        employee_ops.delete_employee(employee)
        self.assertFalse(Employee.objects.filter(pk=employee.pk).exists())
        self.assertFalse(User.objects.filter(pk=user_id).exists())

    def test_cannot_delete_employee_with_appointment(self):
        import datetime as dt

        from apps.clients.models import Client
        from apps.scheduling.models import Appointment

        employee = make_employee(self.tenant)
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        client_ = Client.objects.create(
            tenant=self.tenant, phone="+5511999990000", name="Cliente Teste"
        )
        Appointment.objects.create(
            tenant=self.tenant,
            client=client_,
            employee=employee,
            service=service,
            date=dt.date(2026, 8, 3),
            start_time=dt.time(9, 0),
            end_time=dt.time(10, 0),
            price_at_booking=service.price,
        )
        employee_ops.set_employee_active(employee, False)
        with self.assertRaises(ValidationError):
            employee_ops.delete_employee(employee)
        self.assertTrue(Employee.objects.filter(pk=employee.pk).exists())
        self.assertTrue(User.objects.filter(pk=employee.user_id).exists())


class CommissionPriorityTest(TestCase):
    """Regra 4 do CLAUDE.md: override do vínculo > padrão do funcionário."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant,
            name="Corte",
            duration_minutes=60,
            price=Decimal("100.00"),
        )

    def test_no_link_uses_employee_default(self):
        ctype, cvalue = employee_ops.get_commission_config(
            self.employee, self.service
        )
        self.assertEqual(ctype, CommissionType.PERCENTAGE)
        self.assertEqual(cvalue, Decimal("40.00"))

    def test_link_without_override_uses_employee_default(self):
        employee_ops.link_service(self.employee, self.service)
        ctype, cvalue = employee_ops.get_commission_config(
            self.employee, self.service
        )
        self.assertEqual(ctype, CommissionType.PERCENTAGE)
        self.assertEqual(cvalue, Decimal("40.00"))

    def test_link_with_override_wins(self):
        employee_ops.link_service(
            self.employee,
            self.service,
            commission_type=CommissionType.FIXED,
            commission_value=Decimal("30.00"),
        )
        ctype, cvalue = employee_ops.get_commission_config(
            self.employee, self.service
        )
        self.assertEqual(ctype, CommissionType.FIXED)
        self.assertEqual(cvalue, Decimal("30.00"))

    def test_partial_override_rejected(self):
        with self.assertRaises(ValidationError):
            employee_ops.link_service(
                self.employee,
                self.service,
                commission_type=CommissionType.FIXED,
                commission_value=None,
            )

    def test_cross_tenant_link_rejected(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        other_service = create_service(
            tenant=other_tenant,
            name="Corte B",
            duration_minutes=30,
            price=Decimal("50.00"),
        )
        with self.assertRaises(ValidationError):
            employee_ops.link_service(self.employee, other_service)


class WorkingHoursTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)

    def test_set_working_hours_replaces_schedule(self):
        employee_ops.set_working_hours(
            self.employee,
            [
                {"weekday": 0, "start_time": datetime.time(9), "end_time": datetime.time(18)},
                {"weekday": 1, "start_time": datetime.time(9), "end_time": datetime.time(18)},
            ],
        )
        employee_ops.set_working_hours(
            self.employee,
            [{"weekday": 5, "start_time": datetime.time(10), "end_time": datetime.time(14)}],
        )
        hours = WorkingHours.objects.filter(employee=self.employee)
        self.assertEqual(hours.count(), 1)
        self.assertEqual(hours.first().weekday, 5)

    def test_end_before_start_rejected(self):
        with self.assertRaises(ValidationError):
            employee_ops.set_working_hours(
                self.employee,
                [{"weekday": 0, "start_time": datetime.time(18), "end_time": datetime.time(9)}],
            )


class EmployeePanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_login_required(self):
        response = self.client.get("/painel/funcionarios/")
        self.assertEqual(response.status_code, 302)

    def test_admin_creates_employee(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/funcionarios/novo/",
            {
                "full_name": "Ana Silva",
                "email": "ana@salao.com",
                "password1": "Senha@123",
                "password2": "Senha@123",
                "phone": "",
                "bio": "",
                "default_commission_type": "percentage",
                "default_commission_value": "40.00",
            },
        )
        self.assertEqual(response.status_code, 302)
        employee = Employee.objects.get(full_name="Ana Silva")
        self.assertEqual(employee.tenant, self.tenant)
        self.assertEqual(employee.user.role, User.Role.EMPLOYEE)
        self.assertTrue(employee.user.check_password("Senha@123"))

    def test_create_requires_password(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/funcionarios/novo/",
            {
                "full_name": "Ana Silva",
                "email": "ana@salao.com",
                "password1": "",
                "password2": "",
                "phone": "",
                "bio": "",
                "default_commission_type": "percentage",
                "default_commission_value": "40.00",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Employee.objects.filter(full_name="Ana Silva").exists())

    def test_create_rejects_mismatched_passwords(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/funcionarios/novo/",
            {
                "full_name": "Ana Silva",
                "email": "ana@salao.com",
                "password1": "Senha@123",
                "password2": "Outra@123",
                "phone": "",
                "bio": "",
                "default_commission_type": "percentage",
                "default_commission_value": "40.00",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "não coincidem")
        self.assertFalse(Employee.objects.filter(full_name="Ana Silva").exists())

    def test_edit_without_password_keeps_current_password(self):
        employee = make_employee(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/funcionarios/{employee.pk}/editar/",
            {
                "full_name": "Ana Silva",
                "password1": "",
                "password2": "",
                "phone": "",
                "bio": "",
                "default_commission_type": "percentage",
                "default_commission_value": "40.00",
            },
        )
        self.assertEqual(response.status_code, 302)
        employee.user.refresh_from_db()
        self.assertTrue(employee.user.check_password("Senha@123"))

    def test_edit_with_password_changes_login(self):
        employee = make_employee(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/funcionarios/{employee.pk}/editar/",
            {
                "full_name": "Ana Silva",
                "password1": "NovaSenha@456",
                "password2": "NovaSenha@456",
                "phone": "",
                "bio": "",
                "default_commission_type": "percentage",
                "default_commission_value": "40.00",
            },
        )
        self.assertEqual(response.status_code, 302)
        employee.user.refresh_from_db()
        self.assertTrue(employee.user.check_password("NovaSenha@456"))

    def test_admin_creates_employee_with_comma_decimal_commission(self):
        """Campo de valor de comissão no painel é <input type="text"> (aceita vírgula)."""
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/funcionarios/novo/",
            {
                "full_name": "Bruna Costa",
                "email": "bruna@salao.com",
                "password1": "Senha@123",
                "password2": "Senha@123",
                "phone": "",
                "bio": "",
                "default_commission_type": "percentage",
                "default_commission_value": "37,5",
            },
        )
        self.assertEqual(response.status_code, 302)
        employee = Employee.objects.get(full_name="Bruna Costa")
        self.assertEqual(employee.default_commission_value, Decimal("37.5"))

    def test_working_hours_htmx_post(self):
        employee = make_employee(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/funcionarios/{employee.pk}/jornada/",
            {
                "day_0_active": "on",
                "day_0_start": "09:00",
                "day_0_end": "18:00",
                "day_5_active": "on",
                "day_5_start": "10:00",
                "day_5_end": "14:00",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            WorkingHours.objects.filter(employee=employee).count(), 2
        )

    def test_toggle_confirm_renders_app_modal_not_native(self):
        employee = make_employee(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/funcionarios/{employee.pk}/toggle/confirmar/")
        self.assertContains(response, "Desativar funcionário")
        self.assertContains(response, "Ana Silva")
        self.assertNotContains(response, "hx-confirm")

    def test_toggle_button_opens_modal_instead_of_native_confirm(self):
        employee = make_employee(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/funcionarios/")
        self.assertNotContains(response, "hx-confirm")
        self.assertContains(response, f"funcionarios/{employee.pk}/toggle/confirmar/")

    def test_delete_button_only_rendered_when_inactive(self):
        active = make_employee(self.tenant, email="ativa@salao-a.com", full_name="Ativa")
        inactive = make_employee(
            self.tenant, email="inativa@salao-a.com", full_name="Inativa"
        )
        employee_ops.set_employee_active(inactive, False)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/funcionarios/")
        self.assertContains(
            response, f"/painel/funcionarios/{inactive.pk}/excluir/confirmar/"
        )
        self.assertNotContains(
            response, f"/painel/funcionarios/{active.pk}/excluir/confirmar/"
        )

    def test_delete_inactive_employee_via_panel(self):
        employee = make_employee(self.tenant)
        employee_ops.set_employee_active(employee, False)
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/funcionarios/{employee.pk}/excluir/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Employee.objects.filter(pk=employee.pk).exists())

    def test_delete_active_employee_via_panel_rejected(self):
        employee = make_employee(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/funcionarios/{employee.pk}/excluir/")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Employee.objects.filter(pk=employee.pk).exists())

    def test_services_htmx_post_links_and_overrides(self):
        employee = make_employee(self.tenant)
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/funcionarios/{employee.pk}/servicos/",
            {
                f"service_{service.pk}_linked": "on",
                f"service_{service.pk}_ctype": "fixed",
                f"service_{service.pk}_cvalue": "25.00",
            },
        )
        self.assertEqual(response.status_code, 200)
        ctype, cvalue = employee_ops.get_commission_config(employee, service)
        self.assertEqual(ctype, "fixed")
        self.assertEqual(cvalue, Decimal("25.00"))

    def test_service_override_accepts_comma_decimal_value(self):
        employee = make_employee(self.tenant)
        service = create_service(
            tenant=self.tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/funcionarios/{employee.pk}/servicos/",
            {
                f"service_{service.pk}_linked": "on",
                f"service_{service.pk}_ctype": "fixed",
                f"service_{service.pk}_cvalue": "25,50",
            },
        )
        self.assertEqual(response.status_code, 200)
        ctype, cvalue = employee_ops.get_commission_config(employee, service)
        self.assertEqual(cvalue, Decimal("25.50"))


class EmployeeIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")
        cls.employee_a = make_employee(cls.tenant_a, email="ana@salao-a.com")
        cls.employee_b = make_employee(
            cls.tenant_b, email="bia@salao-b.com", full_name="Bia Souza"
        )

    def test_panel_list_scoped(self):
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/funcionarios/")
        self.assertContains(response, "Ana Silva")
        self.assertNotContains(response, "Bia Souza")

    def test_api_scoped(self):
        client = APIClient()
        client.force_authenticate(self.admin_a)
        names = [e["full_name"] for e in client.get("/api/v1/employees/").json()]
        self.assertEqual(names, ["Ana Silva"])

    def test_panel_cannot_edit_other_tenant_employee(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(
            f"/painel/funcionarios/{self.employee_b.pk}/editar/"
        )
        self.assertEqual(response.status_code, 404)


class EmployeeCommissionHistoryTest(TestCase):
    """Histórico de comissões pagas no perfil do funcionário (só leitura) —
    complementa a aba Comissões do Caixa sem duplicar a lógica de pagamento."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = make_employee(cls.tenant)
        cls.service = create_service(
            tenant=cls.tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
        )
        cls.client_ = Client.objects.create(
            tenant=cls.tenant, phone="+5511999990000", name="Cliente Teste"
        )

    def _commission_for(self, start_time=datetime.time(9, 0), price=Decimal("100.00")):
        appointment = Appointment.objects.create(
            tenant=self.tenant, client=self.client_, employee=self.employee, service=self.service,
            date=datetime.date.today(), start_time=start_time,
            end_time=datetime.time(start_time.hour + 1, 0),
            status=AppointmentStatus.CONFIRMED, price_at_booking=price,
        )
        return finance_ops.create_commission_for_appointment(appointment)

    def test_shows_paid_commission(self):
        commission = self._commission_for()
        finance_ops.mark_commission_paid(commission, payment_method="cash", created_by=self.admin)
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/funcionarios/{self.employee.pk}/editar/")
        self.assertContains(response, "Histórico de comissões pagas")
        self.assertContains(response, "R$ 40,00")

    def test_does_not_show_pending_commission(self):
        self._commission_for()
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/funcionarios/{self.employee.pk}/editar/")
        self.assertContains(response, "Nenhuma comissão paga ainda")

    def test_empty_state_when_no_commissions(self):
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/funcionarios/{self.employee.pk}/editar/")
        self.assertContains(response, "Nenhuma comissão paga ainda")

    def test_does_not_show_other_employees_paid_commissions(self):
        other_employee = make_employee(self.tenant, email="bia@salao-a.com", full_name="Bia Souza")
        other_commission = finance_ops.create_commission_for_appointment(
            Appointment.objects.create(
                tenant=self.tenant, client=self.client_, employee=other_employee, service=self.service,
                date=datetime.date.today(), start_time=datetime.time(11, 0), end_time=datetime.time(12, 0),
                status=AppointmentStatus.CONFIRMED, price_at_booking=Decimal("100.00"),
            )
        )
        finance_ops.mark_commission_paid(other_commission, payment_method="cash", created_by=self.admin)
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/funcionarios/{self.employee.pk}/editar/")
        self.assertContains(response, "Nenhuma comissão paga ainda")


class OwnerEmployeeTest(TestCase):
    """RF: dono marca "também atende" em Configurações e ganha um perfil em
    Funcionários, reaproveitando o próprio login (sem User novo)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_sync_creates_owner_employee_reusing_admin_login(self):
        self.tenant.owner_name = "Carlos Dono"
        self.tenant.owner_attends = True
        self.tenant.save()
        employee = employee_ops.sync_owner_employee(self.tenant)
        self.assertEqual(employee.user_id, self.admin.id)
        self.assertEqual(employee.full_name, "Carlos Dono")
        self.assertTrue(employee.is_owner)
        self.assertTrue(employee.is_active)
        self.assertEqual(User.objects.filter(tenant=self.tenant).count(), 1)

    def test_sync_is_idempotent(self):
        self.tenant.owner_name = "Carlos Dono"
        self.tenant.owner_attends = True
        self.tenant.save()
        employee_ops.sync_owner_employee(self.tenant)
        employee_ops.sync_owner_employee(self.tenant)
        self.assertEqual(Employee.objects.filter(tenant=self.tenant).count(), 1)

    def test_sync_updates_name_and_reactivates_on_resubmit(self):
        self.tenant.owner_name = "Carlos Dono"
        self.tenant.owner_attends = True
        self.tenant.save()
        employee_ops.sync_owner_employee(self.tenant)

        self.tenant.owner_attends = False
        self.tenant.save()
        employee_ops.sync_owner_employee(self.tenant)
        self.assertFalse(Employee.objects.get(tenant=self.tenant).is_active)

        self.tenant.owner_name = "Carlos Silva"
        self.tenant.owner_attends = True
        self.tenant.save()
        employee = employee_ops.sync_owner_employee(self.tenant)
        self.assertTrue(employee.is_active)
        self.assertEqual(employee.full_name, "Carlos Silva")

    def test_no_owner_employee_when_not_attending(self):
        self.tenant.owner_attends = False
        self.tenant.save()
        self.assertIsNone(employee_ops.sync_owner_employee(self.tenant))
        self.assertEqual(Employee.objects.filter(tenant=self.tenant).count(), 0)

    def test_deactivating_owner_employee_never_locks_out_login(self):
        self.tenant.owner_name = "Carlos Dono"
        self.tenant.owner_attends = True
        self.tenant.save()
        employee = employee_ops.sync_owner_employee(self.tenant)
        employee_ops.set_employee_active(employee, False)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_cannot_delete_owner_employee(self):
        self.tenant.owner_name = "Carlos Dono"
        self.tenant.owner_attends = True
        self.tenant.save()
        employee = employee_ops.sync_owner_employee(self.tenant)
        employee_ops.set_employee_active(employee, False)
        with self.assertRaises(ValidationError):
            employee_ops.delete_employee(employee)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_settings_view_syncs_owner_employee(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/configuracoes/",
            self._settings_payload(owner_name="Carlos Dono", owner_attends="on"),
        )
        self.assertEqual(response.status_code, 302)
        employee = Employee.objects.get(tenant=self.tenant, user=self.admin)
        self.assertEqual(employee.full_name, "Carlos Dono")
        self.assertTrue(employee.is_active)

    def test_settings_view_requires_owner_name_when_attending(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/configuracoes/",
            self._settings_payload(owner_name="", owner_attends="on"),
        )
        self.assertContains(response, "Informe seu nome")
        self.assertFalse(Employee.objects.filter(tenant=self.tenant).exists())

    def test_owner_employee_appears_in_panel_list_marked_as_dono(self):
        self.tenant.owner_name = "Carlos Dono"
        self.tenant.owner_attends = True
        self.tenant.save()
        employee_ops.sync_owner_employee(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/funcionarios/")
        self.assertContains(response, "Carlos Dono")
        self.assertContains(response, "Dono")

    def _settings_payload(self, *, owner_name, owner_attends):
        payload = {
            "name": self.tenant.name,
            "slug": self.tenant.slug,
            "theme": "salao",
            "whatsapp": "",
            "address": "",
            "description": "",
            "subscription_due_soon_days": 7,
            "client_inactive_days": 60,
            "owner_name": owner_name,
        }
        if owner_attends:
            payload["owner_attends"] = owner_attends
        return {
            **payload,
            **self._hours_management_form(),
        }

    def _hours_management_form(self):
        data = {
            "form-TOTAL_FORMS": "7",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "7",
        }
        for i in range(7):
            data[f"form-{i}-weekday"] = str(i)
            data[f"form-{i}-is_closed"] = "on"
        return data


class EmployeeSeatLimitTest(TestCase):
    """Plano vira limite de quantos FUNCIONÁRIOS (conta de login própria) o
    tenant pode ter — decisão do usuário em 2026-08-04. O perfil do dono via
    "também atende" nunca conta (ver Employee.is_owner/sync_owner_employee)."""

    @classmethod
    def setUpTestData(cls):
        from apps.billing.models import Plan, Subscription, SubscriptionStatus

        cls.tenant, cls.admin = make_tenant_with_admin("salao-seats")
        cls.plan = Plan.objects.create(
            name="Teste Individual", price=Decimal("49.90"), max_employees=1
        )
        cls.subscription = Subscription.objects.create(
            tenant=cls.tenant, plan=cls.plan, status=SubscriptionStatus.ACTIVE,
        )

    def test_blocks_creation_beyond_plan_limit(self):
        make_employee(self.tenant, email="ana@salao-seats.com")
        with self.assertRaises(ValidationError):
            make_employee(self.tenant, email="bia@salao-seats.com", full_name="Bia")
        self.assertEqual(Employee.objects.filter(tenant=self.tenant).count(), 1)

    def test_error_message_mentions_plan_and_upgrade(self):
        make_employee(self.tenant, email="ana@salao-seats.com")
        with self.assertRaises(ValidationError) as ctx:
            make_employee(self.tenant, email="bia@salao-seats.com", full_name="Bia")
        message = " ".join(ctx.exception.messages)
        self.assertIn("Teste Individual", message)
        self.assertIn("Meu Plano", message)

    def test_no_limit_during_trial_without_plan(self):
        from apps.billing.models import SubscriptionStatus

        self.subscription.plan = None
        self.subscription.status = SubscriptionStatus.TRIALING
        self.subscription.save()
        make_employee(self.tenant, email="ana@salao-seats.com")
        make_employee(self.tenant, email="bia@salao-seats.com", full_name="Bia")
        self.assertEqual(Employee.objects.filter(tenant=self.tenant).count(), 2)

    def test_no_limit_when_plan_max_employees_is_none(self):
        self.plan.max_employees = None
        self.plan.save(update_fields=["max_employees"])
        make_employee(self.tenant, email="ana@salao-seats.com")
        make_employee(self.tenant, email="bia@salao-seats.com", full_name="Bia")
        self.assertEqual(Employee.objects.filter(tenant=self.tenant).count(), 2)

    def test_owner_attending_never_counts_toward_limit(self):
        self.tenant.owner_name = "Dona Catarina"
        self.tenant.owner_attends = True
        self.tenant.save()
        employee_ops.sync_owner_employee(self.tenant)

        # limite=1 e o dono não conta — ainda dá pra criar 1 funcionário real.
        make_employee(self.tenant, email="ana@salao-seats.com")
        with self.assertRaises(ValidationError):
            make_employee(self.tenant, email="bia@salao-seats.com", full_name="Bia")

    def test_deactivated_employee_frees_a_seat(self):
        employee = make_employee(self.tenant, email="ana@salao-seats.com")
        employee_ops.set_employee_active(employee, False)
        make_employee(self.tenant, email="bia@salao-seats.com", full_name="Bia")
        self.assertEqual(
            Employee.objects.filter(tenant=self.tenant, is_active=True).count(), 1
        )

    def test_reactivating_beyond_limit_blocked(self):
        employee = make_employee(self.tenant, email="ana@salao-seats.com")
        employee_ops.set_employee_active(employee, False)
        make_employee(self.tenant, email="bia@salao-seats.com", full_name="Bia")
        with self.assertRaises(ValidationError):
            employee_ops.set_employee_active(employee, True)
        employee.refresh_from_db()
        self.assertFalse(employee.is_active)
