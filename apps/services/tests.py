from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

from . import services as service_ops
from .models import Service

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


class ServiceDomainTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_create_service(self):
        service = service_ops.create_service(
            tenant=self.tenant,
            name="  Corte Feminino  ",
            duration_minutes=60,
            price=Decimal("180.00"),
            description="Lavagem e corte",
        )
        self.assertEqual(service.name, "Corte Feminino")  # strip aplicado
        self.assertEqual(service.tenant, self.tenant)
        self.assertTrue(service.is_active)
        self.assertIsInstance(service.price, Decimal)

    def test_zero_duration_rejected(self):
        with self.assertRaises(ValidationError):
            service_ops.create_service(
                tenant=self.tenant,
                name="Express",
                duration_minutes=0,
                price=Decimal("10.00"),
            )

    def test_negative_price_rejected(self):
        with self.assertRaises(ValidationError):
            service_ops.create_service(
                tenant=self.tenant,
                name="Grátis demais",
                duration_minutes=30,
                price=Decimal("-1.00"),
            )

    def test_update_service(self):
        service = service_ops.create_service(
            tenant=self.tenant,
            name="Manicure",
            duration_minutes=45,
            price=Decimal("45.00"),
        )
        service_ops.update_service(
            service,
            name="Manicure Clássica",
            duration_minutes=50,
            price=Decimal("55.00"),
            description="Cutilagem fina",
        )
        service.refresh_from_db()
        self.assertEqual(service.name, "Manicure Clássica")
        self.assertEqual(service.duration_minutes, 50)
        self.assertEqual(service.price, Decimal("55.00"))

    def test_toggle_active(self):
        service = service_ops.create_service(
            tenant=self.tenant,
            name="Escova",
            duration_minutes=40,
            price=Decimal("70.00"),
        )
        service_ops.set_service_active(service, False)
        service.refresh_from_db()
        self.assertFalse(service.is_active)

    def test_delete_active_service_removes_it(self):
        """Excluir não exige mais desativar antes (decisão do usuário em
        2026-07-29) — o modal de confirmação do painel é a barreira contra
        clique acidental; só PROTECT (agendamento vinculado) bloqueia."""
        service = service_ops.create_service(
            tenant=self.tenant, name="Hidratação", duration_minutes=40, price=Decimal("70.00")
        )
        service_ops.delete_service(service)
        self.assertFalse(Service.objects.filter(pk=service.pk).exists())

    def test_delete_inactive_service_removes_it(self):
        service = service_ops.create_service(
            tenant=self.tenant, name="Progressiva", duration_minutes=40, price=Decimal("70.00")
        )
        service_ops.set_service_active(service, False)
        service_ops.delete_service(service)
        self.assertFalse(Service.objects.filter(pk=service.pk).exists())


class ServiceIsolationTest(TestCase):
    """Regra #1: serviço de um tenant nunca aparece para outro."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")
        cls.service_a = service_ops.create_service(
            tenant=cls.tenant_a, name="Corte A", duration_minutes=60, price=Decimal("100")
        )
        cls.service_b = service_ops.create_service(
            tenant=cls.tenant_b, name="Corte B", duration_minutes=60, price=Decimal("100")
        )

    def test_for_tenant_scopes_services(self):
        names_a = list(
            Service.objects.for_tenant(self.tenant_a).values_list("name", flat=True)
        )
        self.assertEqual(names_a, ["Corte A"])

    def test_api_list_only_own_tenant(self):
        client = APIClient()
        client.force_authenticate(self.admin_a)
        response = client.get("/api/v1/services/")
        self.assertEqual(response.status_code, 200)
        names = [item["name"] for item in response.json()]
        self.assertEqual(names, ["Corte A"])

    def test_api_cannot_access_other_tenant_service(self):
        client = APIClient()
        client.force_authenticate(self.admin_a)
        response = client.get(f"/api/v1/services/{self.service_b.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_panel_list_only_own_tenant(self):
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/servicos/")
        self.assertContains(response, "Corte A")
        self.assertNotContains(response, "Corte B")


class ServiceAPIPermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = User.objects.create_user(
            email="func@salao-a.com",
            password="x",
            role=User.Role.EMPLOYEE,
            tenant=cls.tenant,
        )

    def test_anonymous_denied(self):
        response = APIClient().get("/api/v1/services/")
        self.assertEqual(response.status_code, 403)

    def test_employee_can_read_but_not_write(self):
        client = APIClient()
        client.force_authenticate(self.employee)
        self.assertEqual(client.get("/api/v1/services/").status_code, 200)
        response = client.post(
            "/api/v1/services/",
            {"name": "Novo", "duration_minutes": 30, "price": "50.00"},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_create(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post(
            "/api/v1/services/",
            {"name": "Progressiva", "duration_minutes": 120, "price": "300.00"},
        )
        self.assertEqual(response.status_code, 201)
        service = Service.objects.get(pk=response.json()["id"])
        self.assertEqual(service.tenant, self.tenant)

    def test_admin_can_delete_active_service(self):
        service = service_ops.create_service(
            tenant=self.tenant, name="Ativo", duration_minutes=30, price=Decimal("50")
        )
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.delete(f"/api/v1/services/{service.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Service.objects.filter(pk=service.pk).exists())

    def test_admin_can_delete_inactive_service(self):
        service = service_ops.create_service(
            tenant=self.tenant, name="Inativo", duration_minutes=30, price=Decimal("50")
        )
        service_ops.set_service_active(service, False)
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.delete(f"/api/v1/services/{service.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Service.objects.filter(pk=service.pk).exists())


class ServicePanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = User.objects.create_user(
            email="func@salao-a.com",
            password="x",
            role=User.Role.EMPLOYEE,
            tenant=cls.tenant,
        )

    def test_login_required(self):
        response = self.client.get("/painel/servicos/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/painel/login/", response.url)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee)
        response = self.client.get("/painel/servicos/")
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_service_via_htmx(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/servicos/novo/",
            {"name": "Coloração", "description": "", "duration_minutes": 120, "price": "350.00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Service.objects.for_tenant(self.tenant)
            .filter(name="Coloração")
            .exists()
        )
        self.assertContains(response, "Coloração")

    def test_admin_creates_service_with_comma_decimal_price(self):
        """Campo de preço no painel é <input type="text"> (aceita vírgula)."""
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/servicos/novo/",
            {"name": "Progressiva", "description": "", "duration_minutes": 90, "price": "350,50"},
        )
        self.assertEqual(response.status_code, 200)
        service = Service.objects.get(tenant=self.tenant, name="Progressiva")
        self.assertEqual(service.price, Decimal("350.50"))

    def test_invalid_form_reopens_modal(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/servicos/novo/",
            {"name": "", "duration_minutes": 0, "price": "-5"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Retarget"), "#modal-slot")
        self.assertEqual(Service.objects.count(), 0)

    def test_toggle(self):
        service = service_ops.create_service(
            tenant=self.tenant, name="Escova", duration_minutes=40, price=Decimal("70")
        )
        self.client.force_login(self.admin)
        self.client.post(f"/painel/servicos/{service.pk}/toggle/")
        service.refresh_from_db()
        self.assertFalse(service.is_active)

    def test_toggle_confirm_renders_app_modal_not_native(self):
        service = service_ops.create_service(
            tenant=self.tenant, name="Escova", duration_minutes=40, price=Decimal("70")
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/servicos/{service.pk}/toggle/confirmar/")
        self.assertContains(response, "Desativar serviço")
        self.assertContains(response, "Escova")
        # não deve depender do confirm() nativo do navegador
        self.assertNotContains(response, "hx-confirm")

    def test_toggle_button_opens_modal_instead_of_native_confirm(self):
        service = service_ops.create_service(
            tenant=self.tenant, name="Escova", duration_minutes=40, price=Decimal("70")
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/servicos/")
        self.assertNotContains(response, "hx-confirm")
        self.assertContains(response, f"servicos/{service.pk}/toggle/confirmar/")

    def test_delete_button_rendered_regardless_of_status(self):
        """Excluir está sempre disponível (com modal de confirmação) — não é
        mais preciso desativar antes (decisão do usuário em 2026-07-29)."""
        active = service_ops.create_service(
            tenant=self.tenant, name="Corte Ativo", duration_minutes=40, price=Decimal("70")
        )
        inactive = service_ops.create_service(
            tenant=self.tenant, name="Corte Inativo", duration_minutes=40, price=Decimal("70")
        )
        service_ops.set_service_active(inactive, False)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/servicos/")
        self.assertContains(
            response, f'/painel/servicos/{inactive.pk}/excluir/confirmar/'
        )
        self.assertContains(
            response, f'/painel/servicos/{active.pk}/excluir/confirmar/'
        )

    def test_delete_confirm_renders_modal(self):
        service = service_ops.create_service(
            tenant=self.tenant, name="Escova", duration_minutes=40, price=Decimal("70")
        )
        service_ops.set_service_active(service, False)
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/servicos/{service.pk}/excluir/confirmar/")
        self.assertContains(response, "Excluir serviço")
        self.assertContains(response, "Escova")

    def test_delete_inactive_service_via_panel(self):
        service = service_ops.create_service(
            tenant=self.tenant, name="Escova", duration_minutes=40, price=Decimal("70")
        )
        service_ops.set_service_active(service, False)
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/servicos/{service.pk}/excluir/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Service.objects.filter(pk=service.pk).exists())

    def test_delete_active_service_via_panel_succeeds(self):
        service = service_ops.create_service(
            tenant=self.tenant, name="Escova", duration_minutes=40, price=Decimal("70")
        )
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/servicos/{service.pk}/excluir/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Service.objects.filter(pk=service.pk).exists())


class BookableServicesTest(TestCase):
    """RF14 — serviço só aparece na página pública se ativo E com funcionário
    ativo vinculado."""

    @classmethod
    def setUpTestData(cls):
        from apps.employees.services import create_employee, link_service

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.linked = service_ops.create_service(
            tenant=cls.tenant, name="Com Funcionário", duration_minutes=30, price=Decimal("50")
        )
        cls.unlinked = service_ops.create_service(
            tenant=cls.tenant, name="Sem Funcionário", duration_minutes=30, price=Decimal("50")
        )
        cls.inactive = service_ops.create_service(
            tenant=cls.tenant, name="Inativo", duration_minutes=30, price=Decimal("50")
        )
        service_ops.set_service_active(cls.inactive, False)
        cls.employee = create_employee(
            tenant=cls.tenant,
            full_name="Ana Silva",
            email="ana@salao-a.com",
            password="Senha@123",
            default_commission_type="percentage",
            default_commission_value=Decimal("40.00"),
        )
        link_service(cls.employee, cls.linked)
        link_service(cls.employee, cls.inactive)

    def test_only_active_linked_service_is_bookable(self):
        names = list(
            service_ops.bookable_services(self.tenant).values_list("name", flat=True)
        )
        self.assertEqual(names, ["Com Funcionário"])

    def test_inactive_service_not_bookable_even_if_linked(self):
        self.assertNotIn(
            "Inativo",
            service_ops.bookable_services(self.tenant).values_list("name", flat=True),
        )

    def test_active_service_without_linked_employee_not_bookable(self):
        self.assertNotIn(
            "Sem Funcionário",
            service_ops.bookable_services(self.tenant).values_list("name", flat=True),
        )

    def test_inactive_employee_removes_service_from_public_page(self):
        from apps.employees.services import set_employee_active

        set_employee_active(self.employee, False)
        self.assertEqual(service_ops.bookable_services(self.tenant).count(), 0)


class ServiceListBookabilityWarningTest(TestCase):
    """Achado real num tenant de produção em 2026-08-09: serviço ativo sem
    funcionário vinculado some da página pública (RF14) mas o admin
    continua conseguindo agendar com ele manualmente (RF17, "encaixe" não
    exige o vínculo) — sem aviso nenhum, o admin só descobria o problema
    quando um cliente reclamasse. `/painel/servicos/` agora avisa direto na
    lista."""

    @classmethod
    def setUpTestData(cls):
        from apps.employees.services import create_employee, link_service

        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.linked = service_ops.create_service(
            tenant=cls.tenant, name="Com Funcionário", duration_minutes=30, price=Decimal("50")
        )
        cls.unlinked = service_ops.create_service(
            tenant=cls.tenant, name="Sem Funcionário", duration_minutes=30, price=Decimal("50")
        )
        cls.inactive_unlinked = service_ops.create_service(
            tenant=cls.tenant, name="Inativo Sem Funcionário", duration_minutes=30, price=Decimal("50")
        )
        service_ops.set_service_active(cls.inactive_unlinked, False)
        cls.employee = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="ana@salao-a.com",
            password="Senha@123", default_commission_type="percentage",
            default_commission_value=Decimal("40.00"),
        )
        link_service(cls.employee, cls.linked)

    def test_warns_only_for_active_unlinked_service(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/servicos/")
        self.assertContains(response, "Sem profissional vinculado")
        # o aviso aparece 1x só (pro "Sem Funcionário") — não pro linkado
        # nem pro inativo (que já mostra "Inativo", não precisa dos dois).
        self.assertContains(response, "Sem profissional vinculado", count=1)

    def test_no_warning_once_employee_is_linked(self):
        from apps.employees.services import link_service

        link_service(self.employee, self.unlinked)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/servicos/")
        self.assertNotContains(response, "Sem profissional vinculado")
