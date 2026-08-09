import datetime
from decimal import Decimal

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, models
from django.http import Http404, HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient

from apps.clients.models import Client

from .forms import BRDecimalField
from .middleware import TenantMiddleware
from .models import Tenant, TenantBusinessHours, TenantManager, TenantModel, Weekday
from .services import (
    create_default_business_hours,
    delete_tenant_account,
    register_tenant,
    set_business_hours,
    theme_from_host,
)

User = get_user_model()

LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class IsolationProbe(TenantModel):
    """Model concreta usada só nos testes para exercitar TenantModel/TenantManager.

    Não gera migration: a tabela é criada/destruída via schema_editor em
    `setUpModule`/`tearDownModule` (não por classe!) — o model fica
    registrado no app registry durante TODO o módulo de testes, então
    `Tenant.delete()` de QUALQUER outra classe deste arquivo (ex.:
    `delete_tenant_account`) tentaria fazer CASCADE para a tabela dele mesmo
    fora do teste de isolamento, e falharia com "relation does not exist" se
    a tabela só existisse durante uma classe específica.

    Idempotente (checa antes de criar/apagar): outros módulos de teste que
    também chamam `delete_tenant_account` (ex.: apps.billing.tests) rodam em
    ordem alfabética ANTES deste — "billing" < "tenants" — então precisam
    criar essa mesma tabela antecipadamente pra não estourar
    "relation does not exist" no cascade. Sem o check, o segundo
    `setUpModule` a rodar (o deste arquivo) tentaria criar de novo e
    quebraria com "relation already exists".
    """

    name = models.CharField(max_length=50)

    class Meta:
        app_label = "tenants"


def _isolation_probe_table_exists():
    return IsolationProbe._meta.db_table in connection.introspection.table_names()


def setUpModule():
    if not _isolation_probe_table_exists():
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(IsolationProbe)


def tearDownModule():
    if _isolation_probe_table_exists():
        with connection.schema_editor() as schema_editor:
            schema_editor.delete_model(IsolationProbe)


class TenantIsolationTest(TestCase):
    """Teste-guarda da regra #1: dado de um tenant nunca aparece para outro."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.tenant_b = Tenant.objects.create(name="Salão B", slug="salao-b")
        IsolationProbe.objects.create(tenant=cls.tenant_a, name="dado do A")
        IsolationProbe.objects.create(tenant=cls.tenant_a, name="outro dado do A")
        IsolationProbe.objects.create(tenant=cls.tenant_b, name="dado do B")

    def test_tenant_model_uses_tenant_manager(self):
        self.assertIsInstance(IsolationProbe.objects, TenantManager)

    def test_for_tenant_returns_only_own_rows(self):
        rows_a = IsolationProbe.objects.for_tenant(self.tenant_a)
        rows_b = IsolationProbe.objects.for_tenant(self.tenant_b)

        self.assertEqual(rows_a.count(), 2)
        self.assertEqual(rows_b.count(), 1)
        self.assertQuerySetEqual(
            rows_a.order_by("name").values_list("name", flat=True),
            ["dado do A", "outro dado do A"],
            transform=str,
        )

    def test_tenant_a_never_sees_tenant_b_data(self):
        names_seen_by_a = set(
            IsolationProbe.objects.for_tenant(self.tenant_a).values_list(
                "name", flat=True
            )
        )
        self.assertNotIn("dado do B", names_seen_by_a)

    def test_for_tenant_chains_with_other_filters(self):
        qs = IsolationProbe.objects.for_tenant(self.tenant_b).filter(
            name__icontains="dado"
        )
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().tenant, self.tenant_b)

    def test_deleting_tenant_cascades_own_rows_only(self):
        self.tenant_b.delete()
        self.assertEqual(IsolationProbe.objects.count(), 2)
        self.assertFalse(
            IsolationProbe.objects.filter(name="dado do B").exists()
        )


class TenantMiddlewareTest(TestCase):
    factory = RequestFactory()

    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.tenant_b = Tenant.objects.create(name="Salão B", slug="salao-b")
        cls.inactive = Tenant.objects.create(
            name="Salão Fechado", slug="salao-fechado", is_active=False
        )
        cls.admin_a = User.objects.create_user(
            email="dona@salao-a.com",
            password="x",
            role=User.Role.TENANT_ADMIN,
            tenant=cls.tenant_a,
        )

    def _run_middleware(self, request, view_kwargs=None):
        middleware = TenantMiddleware(lambda req: HttpResponse())
        middleware(request)
        if view_kwargs is not None:
            middleware.process_view(request, lambda r: None, (), view_kwargs)
        return request

    def test_anonymous_request_has_no_tenant(self):
        request = self.factory.get("/")
        request.user = AnonymousUser()
        self._run_middleware(request)
        self.assertIsNone(request.tenant)

    def test_logged_user_resolves_own_tenant(self):
        request = self.factory.get("/painel/")
        request.user = self.admin_a
        self._run_middleware(request)
        self.assertEqual(request.tenant, self.tenant_a)

    def test_public_url_resolves_tenant_by_slug(self):
        request = self.factory.get("/salao-b/")
        request.user = AnonymousUser()
        self._run_middleware(request, view_kwargs={"tenant_slug": "salao-b"})
        self.assertEqual(request.tenant, self.tenant_b)

    def test_slug_takes_precedence_over_logged_user_tenant(self):
        request = self.factory.get("/salao-b/")
        request.user = self.admin_a
        self._run_middleware(request, view_kwargs={"tenant_slug": "salao-b"})
        self.assertEqual(request.tenant, self.tenant_b)

    def test_unknown_slug_raises_404(self):
        request = self.factory.get("/nao-existe/")
        request.user = AnonymousUser()
        with self.assertRaises(Http404):
            self._run_middleware(request, view_kwargs={"tenant_slug": "nao-existe"})

    def test_inactive_tenant_raises_404(self):
        request = self.factory.get("/salao-fechado/")
        request.user = AnonymousUser()
        with self.assertRaises(Http404):
            self._run_middleware(
                request, view_kwargs={"tenant_slug": "salao-fechado"}
            )


class BRDecimalFieldTest(TestCase):
    """Campos de dinheiro/quantidade no painel usam <input type="text"> (não
    <input type="number">, que só aceita ponto) — este field aceita os dois
    formatos que um usuário BR pode digitar."""

    def setUp(self):
        self.field = BRDecimalField(max_digits=10, decimal_places=2)

    def test_accepts_comma_as_decimal_separator(self):
        self.assertEqual(self.field.clean("10,50"), Decimal("10.50"))

    def test_accepts_period_as_decimal_separator(self):
        self.assertEqual(self.field.clean("10.50"), Decimal("10.50"))

    def test_accepts_thousands_separator_with_comma_decimal(self):
        self.assertEqual(self.field.clean("1.234,56"), Decimal("1234.56"))

    def test_accepts_integer_without_separator(self):
        self.assertEqual(self.field.clean("100"), Decimal("100"))

    def test_invalid_value_still_rejected(self):
        from django import forms as django_forms

        with self.assertRaises(django_forms.ValidationError):
            self.field.clean("abc")


# GIF 1x1 mínimo válido — o ImageField exige um arquivo de imagem de verdade.
_TINY_GIF = (
    b"GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc,\x00\x00"
    b"\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


class TenantWhatsappNumberTest(TestCase):
    """`Tenant.whatsapp_wa_me_number` — usado pra montar o link de aviso de
    cancelamento (RF06c)."""

    def test_cleans_formatting_and_adds_ddi(self):
        tenant = Tenant.objects.create(name="Salão A", slug="salao-a", whatsapp="(11) 90000-0000")
        self.assertEqual(tenant.whatsapp_wa_me_number, "5511900000000")

    def test_keeps_existing_ddi(self):
        tenant = Tenant.objects.create(name="Salão B", slug="salao-b", whatsapp="+55 11 90000-0000")
        self.assertEqual(tenant.whatsapp_wa_me_number, "5511900000000")

    def test_none_when_empty(self):
        tenant = Tenant.objects.create(name="Salão C", slug="salao-c", whatsapp="")
        self.assertIsNone(tenant.whatsapp_wa_me_number)


class TenantSettingsPanelTest(TestCase):
    """RF25-RF27 — /painel/configuracoes/."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.admin = User.objects.create_user(
            email="dona@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=cls.tenant,
        )

    def _hours_payload(self):
        """Dados de management form + 7 dias exigidos pelo BusinessHoursFormSet
        — sem isso o formset acusa "ManagementForm data is missing"."""
        data = {
            "form-TOTAL_FORMS": "7",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for weekday in range(7):
            data[f"form-{weekday}-weekday"] = str(weekday)
            if weekday == 6:  # domingo fechado por padrão no payload de teste
                data[f"form-{weekday}-is_closed"] = "on"
            else:
                data[f"form-{weekday}-start_time"] = "09:00"
                data[f"form-{weekday}-end_time"] = "18:00"
        return data

    def _valid_payload(self, **overrides):
        payload = {
            "name": "Salão A Renovado",
            "slug": "salao-a",
            "theme": "salao",
            "whatsapp": "+5511999990000",
            "address": "Rua Nova, 100",
            "description": "Descrição atualizada",
            "subscription_due_soon_days": "7",
            "client_inactive_days": "60",
        }
        payload.update(self._hours_payload())
        payload.update(overrides)
        return payload

    def test_login_required(self):
        response = self.client.get("/painel/configuracoes/")
        self.assertEqual(response.status_code, 302)

    def test_get_shows_current_values(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/configuracoes/")
        self.assertContains(response, "Salão A")
        self.assertContains(response, "salao-a")

    def test_admin_updates_settings(self):
        self.client.force_login(self.admin)
        response = self.client.post("/painel/configuracoes/", self._valid_payload())
        self.assertEqual(response.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.name, "Salão A Renovado")
        self.assertEqual(self.tenant.whatsapp, "+5511999990000")
        monday = TenantBusinessHours.objects.get(tenant=self.tenant, weekday=0)
        self.assertFalse(monday.is_closed)
        self.assertEqual(monday.start_time, datetime.time(9, 0))
        self.assertEqual(monday.end_time, datetime.time(18, 0))
        sunday = TenantBusinessHours.objects.get(tenant=self.tenant, weekday=6)
        self.assertTrue(sunday.is_closed)
        self.assertIsNone(sunday.start_time)

    def test_successful_save_marks_page_for_saved_modal(self):
        """Página pós-redirect carrega o marcador que o Alpine usa pra abrir
        o modal "Salvo!" automaticamente (ver settings.html)."""
        self.client.force_login(self.admin)
        response = self.client.post("/painel/configuracoes/", self._valid_payload(), follow=True)
        self.assertContains(response, 'id="settings-just-saved"')

    def test_validation_error_does_not_mark_page_for_saved_modal(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/configuracoes/", self._valid_payload(slug=""), follow=True
        )
        self.assertNotContains(response, 'id="settings-just-saved"')

    def test_whatsapp_cancel_redirect_defaults_true_and_can_be_turned_off(self):
        self.assertTrue(self.tenant.whatsapp_cancel_redirect_enabled)
        self.client.force_login(self.admin)
        # checkbox ausente no payload = desmarcado
        self.client.post("/painel/configuracoes/", self._valid_payload())
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.whatsapp_cancel_redirect_enabled)

    def test_whatsapp_cancel_redirect_stays_on_when_checkbox_sent(self):
        self.client.force_login(self.admin)
        self.client.post(
            "/painel/configuracoes/",
            self._valid_payload(whatsapp_cancel_redirect_enabled="on"),
        )
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.whatsapp_cancel_redirect_enabled)

    def test_birthday_alert_defaults_off_and_can_be_turned_on(self):
        self.assertFalse(self.tenant.birthday_alert_enabled)
        self.client.force_login(self.admin)
        self.client.post(
            "/painel/configuracoes/", self._valid_payload(birthday_alert_enabled="on"),
        )
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.birthday_alert_enabled)

    def test_birthday_alert_off_when_checkbox_not_sent(self):
        self.tenant.birthday_alert_enabled = True
        self.tenant.save(update_fields=["birthday_alert_enabled"])
        self.client.force_login(self.admin)
        self.client.post("/painel/configuracoes/", self._valid_payload())
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.birthday_alert_enabled)

    def test_admin_changes_theme_to_barbearia(self):
        self.assertEqual(self.tenant.theme, "salao")
        self.client.force_login(self.admin)
        self.client.post("/painel/configuracoes/", self._valid_payload(theme="barbearia"))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.theme, "barbearia")

    def test_slug_uniqueness_against_other_tenant(self):
        Tenant.objects.create(name="Salão B", slug="salao-b")
        self.client.force_login(self.admin)
        response = self.client.post("/painel/configuracoes/", self._valid_payload(slug="salao-b"))
        self.assertEqual(response.status_code, 200)  # form reexibida com erro
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.slug, "salao-a")  # não mudou

    def test_slug_can_keep_its_own_value(self):
        """Reenviar o próprio slug não deve disparar erro de unicidade."""
        self.client.force_login(self.admin)
        response = self.client.post("/painel/configuracoes/", self._valid_payload(slug="salao-a"))
        self.assertEqual(response.status_code, 302)

    def test_reserved_slug_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post("/painel/configuracoes/", self._valid_payload(slug="painel"))
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.slug, "salao-a")

    def test_logo_upload(self):
        self.client.force_login(self.admin)
        logo = SimpleUploadedFile("logo.gif", _TINY_GIF, content_type="image/gif")
        payload = self._valid_payload()
        payload["logo"] = logo
        response = self.client.post("/painel/configuracoes/", payload)
        self.assertEqual(response.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.logo)

    def test_employee_forbidden(self):
        User = get_user_model()
        employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=self.tenant,
        )
        self.client.force_login(employee)
        response = self.client.get("/painel/configuracoes/")
        self.assertEqual(response.status_code, 403)

    def test_isolation_admin_cannot_affect_other_tenant(self):
        other_tenant = Tenant.objects.create(name="Salão B", slug="salao-b")
        self.client.force_login(self.admin)
        self.client.post("/painel/configuracoes/", self._valid_payload())
        other_tenant.refresh_from_db()
        self.assertEqual(other_tenant.name, "Salão B")


class BusinessHoursDomainTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")

    def test_create_default_business_hours_creates_all_7_days(self):
        create_default_business_hours(self.tenant)
        rows = TenantBusinessHours.objects.filter(tenant=self.tenant).order_by("weekday")
        self.assertEqual(rows.count(), 7)
        self.assertEqual([r.weekday for r in rows], list(range(7)))
        sunday = rows.get(weekday=Weekday.SUNDAY)
        self.assertTrue(sunday.is_closed)

    def test_register_tenant_creates_default_business_hours(self):
        tenant, _ = register_tenant(
            name="Salão Novo", email="dono@salao-novo.com", password="Senha@123"
        )
        self.assertEqual(TenantBusinessHours.objects.filter(tenant=tenant).count(), 7)

    def test_set_business_hours_upserts_existing_rows(self):
        create_default_business_hours(self.tenant)
        set_business_hours(
            self.tenant,
            [
                {
                    "weekday": 0, "is_closed": False,
                    "start_time": datetime.time(10, 0), "end_time": datetime.time(20, 0),
                },
            ],
        )
        monday = TenantBusinessHours.objects.get(tenant=self.tenant, weekday=0)
        self.assertEqual(monday.start_time, datetime.time(10, 0))
        self.assertEqual(monday.end_time, datetime.time(20, 0))
        self.assertEqual(TenantBusinessHours.objects.filter(tenant=self.tenant).count(), 7)

    def test_set_business_hours_closing_clears_times(self):
        create_default_business_hours(self.tenant)
        set_business_hours(self.tenant, [{"weekday": 0, "is_closed": True}])
        monday = TenantBusinessHours.objects.get(tenant=self.tenant, weekday=0)
        self.assertTrue(monday.is_closed)
        self.assertIsNone(monday.start_time)
        self.assertIsNone(monday.end_time)

    def test_set_business_hours_rejects_missing_times_when_open(self):
        create_default_business_hours(self.tenant)
        with self.assertRaises(ValidationError):
            set_business_hours(self.tenant, [{"weekday": 0, "is_closed": False}])

    def test_set_business_hours_rejects_end_before_start(self):
        create_default_business_hours(self.tenant)
        with self.assertRaises(ValidationError):
            set_business_hours(
                self.tenant,
                [
                    {
                        "weekday": 0, "is_closed": False,
                        "start_time": datetime.time(18, 0), "end_time": datetime.time(9, 0),
                    },
                ],
            )

    def test_isolation_scoped_per_tenant(self):
        other_tenant = Tenant.objects.create(name="Salão B", slug="salao-b")
        create_default_business_hours(self.tenant)
        create_default_business_hours(other_tenant)
        set_business_hours(
            other_tenant,
            [
                {
                    "weekday": 0, "is_closed": False,
                    "start_time": datetime.time(6, 0), "end_time": datetime.time(7, 0),
                },
            ],
        )
        monday_a = TenantBusinessHours.objects.get(tenant=self.tenant, weekday=0)
        self.assertNotEqual(monday_a.start_time, datetime.time(6, 0))


class PublicBusinessHoursTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        create_default_business_hours(cls.tenant)

    def test_public_page_lists_all_7_days(self):
        response = self.client.get("/salao-a/")
        for label in [
            "Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira",
            "Sexta-feira", "Sábado", "Domingo",
        ]:
            self.assertContains(response, label)

    def test_public_page_shows_closed_day(self):
        response = self.client.get("/salao-a/")
        self.assertContains(response, "Fechado")

    def test_public_page_hides_section_without_hours(self):
        empty_tenant = Tenant.objects.create(name="Salão Vazio", slug="salao-vazio")
        response = self.client.get("/salao-vazio/")
        self.assertNotContains(response, "Segunda-feira")


class TenantThemeRenderingTest(TestCase):
    """A troca de tema é config-only (paleta/tipografia embutida em
    `_theme_tailwind_config.html`/`_theme_fonts.html`, incluída nos 4
    templates "donos" de `tailwind.config`) — confirma que a cor/fonte
    certa aparece no HTML renderizado, nos dois lados do app."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.admin = User.objects.create_user(
            email="dona@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=cls.tenant,
        )

    def test_salao_theme_on_public_home(self):
        response = self.client.get("/salao-a/")
        self.assertContains(response, "#7d562d")
        self.assertContains(response, "Playfair")
        self.assertNotContains(response, "#fbba64")

    def test_barbearia_theme_on_public_home(self):
        self.tenant.theme = "barbearia"
        self.tenant.save(update_fields=["theme"])
        response = self.client.get("/salao-a/")
        self.assertContains(response, "#fbba64")
        self.assertContains(response, "Archivo")
        self.assertNotContains(response, "#7d562d")

    def test_barbearia_theme_on_public_booking_wizard(self):
        self.tenant.theme = "barbearia"
        self.tenant.save(update_fields=["theme"])
        response = self.client.get("/salao-a/agendar/")
        self.assertContains(response, "#fbba64")

    def test_barbearia_theme_on_painel(self):
        self.tenant.theme = "barbearia"
        self.tenant.save(update_fields=["theme"])
        self.client.force_login(self.admin)
        response = self.client.get("/painel/servicos/")
        self.assertContains(response, "#fbba64")
        self.assertContains(response, "Archivo")

    def test_salao_theme_on_painel_default(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/servicos/")
        self.assertContains(response, "#7d562d")
        self.assertNotContains(response, "#fbba64")


class TenantSettingsAPITest(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.admin = User.objects.create_user(
            email="dona@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=cls.tenant,
        )
        cls.employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant,
        )

    def test_anonymous_denied(self):
        response = APIClient().get("/api/v1/tenant-settings/")
        self.assertEqual(response.status_code, 403)

    def test_employee_can_read(self):
        client = APIClient()
        client.force_authenticate(self.employee)
        response = client.get("/api/v1/tenant-settings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["slug"], "salao-a")

    def test_employee_cannot_update(self):
        client = APIClient()
        client.force_authenticate(self.employee)
        response = client.patch("/api/v1/tenant-settings/", {"name": "Hackeado"})
        self.assertEqual(response.status_code, 403)

    def test_admin_can_update(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.patch("/api/v1/tenant-settings/", {"name": "Novo Nome"})
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.name, "Novo Nome")


class RegisterTenantDomainTest(TestCase):
    def test_creates_tenant_and_admin(self):
        tenant, user = register_tenant(
            name="Espaço Beleza", email="dona@espacobeleza.com", password="senhaSegura123"
        )
        self.assertEqual(tenant.name, "Espaço Beleza")
        self.assertEqual(tenant.slug, "espaco-beleza")
        self.assertTrue(tenant.is_active)
        self.assertEqual(user.role, User.Role.TENANT_ADMIN)
        self.assertEqual(user.tenant, tenant)
        self.assertTrue(user.check_password("senhaSegura123"))

    def test_slug_collision_gets_suffix(self):
        Tenant.objects.create(name="Espaço Beleza", slug="espaco-beleza")
        tenant, _ = register_tenant(
            name="Espaço Beleza", email="outra@espacobeleza.com", password="senhaSegura123"
        )
        self.assertEqual(tenant.slug, "espaco-beleza-2")

    def test_reserved_word_name_does_not_produce_reserved_slug(self):
        tenant, _ = register_tenant(
            name="Painel", email="dona@painel.com", password="senhaSegura123"
        )
        self.assertNotEqual(tenant.slug, "painel")

    def test_duplicate_email_rejected(self):
        User.objects.create_user(
            email="ja@existe.com", password="x", role=User.Role.TENANT_ADMIN,
            tenant=Tenant.objects.create(name="Outro", slug="outro"),
        )
        with self.assertRaises(ValidationError):
            register_tenant(name="Novo Salão", email="ja@existe.com", password="senhaSegura123")
        self.assertFalse(Tenant.objects.filter(name="Novo Salão").exists())

    def test_empty_name_rejected(self):
        with self.assertRaises(ValidationError):
            register_tenant(name="   ", email="dona@x.com", password="senhaSegura123")

    def test_defaults_to_salao_theme(self):
        tenant, _ = register_tenant(
            name="Espaço Beleza", email="dona2@espacobeleza.com", password="senhaSegura123",
        )
        self.assertEqual(tenant.theme, "salao")

    def test_accepts_explicit_theme(self):
        tenant, _ = register_tenant(
            name="Barbearia do Zé", email="ze@barbearia.com", password="senhaSegura123",
            theme="barbearia",
        )
        self.assertEqual(tenant.theme, "barbearia")


class TenantThemeModelTest(TestCase):
    def test_defaults_to_salao(self):
        tenant = Tenant.objects.create(name="Salão X", slug="salao-x")
        self.assertEqual(tenant.theme, "salao")


class ThemeFromHostTest(TestCase):
    """Detecção de tema pelo subdomínio (decisão do usuário em 2026-08-03)
    — `barbearia.`/`salao.` são os 2 pontos de entrada dedicados; qualquer
    outro host (domínio raiz, dev local sem subdomínio) não tem tema."""

    def test_detects_barbearia_subdomain(self):
        self.assertEqual(theme_from_host("barbearia.zellup.com.br"), "barbearia")

    def test_detects_salao_subdomain(self):
        self.assertEqual(theme_from_host("salao.zellup.com.br"), "salao")

    def test_root_domain_has_no_theme(self):
        self.assertIsNone(theme_from_host("zellup.com.br"))

    def test_plain_localhost_has_no_theme(self):
        self.assertIsNone(theme_from_host("localhost:8000"))

    def test_detects_subdomain_with_port_for_local_testing(self):
        self.assertEqual(theme_from_host("barbearia.localhost:8000"), "barbearia")


class ChooseThemeViewTest(TestCase):
    """`/painel/escolher-tema/` — tela pra quem se cadastrou via Google
    confirmar Salão de Beleza/Barbearia (decisão do usuário em 2026-08-02).
    """

    def setUp(self):
        User = get_user_model()
        self.tenant = Tenant.objects.create(
            name="Salão de Julia", slug="salao-de-julia", theme_confirmed=False
        )
        self.admin = User.objects.create_user(
            email="julia@gmail.com", password="x",
            role=User.Role.TENANT_ADMIN, tenant=self.tenant,
        )
        self.client.force_login(self.admin)

    def test_get_renders_form(self):
        response = self.client.get("/painel/escolher-tema/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Salão de Beleza")
        self.assertContains(response, "Barbearia")

    def test_post_confirms_theme_and_redirects_to_painel(self):
        response = self.client.post("/painel/escolher-tema/", {"theme": "barbearia"})
        self.assertRedirects(response, "/painel/", fetch_redirect_response=False)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.theme, "barbearia")
        self.assertTrue(self.tenant.theme_confirmed)

    def test_post_invalid_theme_rejected(self):
        response = self.client.post("/painel/escolher-tema/", {"theme": "spa-de-luxo"})
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.theme_confirmed)

    def test_confirmed_tenant_can_still_change_mind_here(self):
        """Não é só pra quem está pendente — acessível a qualquer momento,
        mesmo já confirmado (ex: usuário volta na URL por engano)."""
        self.tenant.theme_confirmed = True
        self.tenant.save(update_fields=["theme_confirmed"])
        response = self.client.get("/painel/escolher-tema/")
        self.assertEqual(response.status_code, 200)


@override_settings(CACHES=LOCMEM_CACHE)
class SignUpViewTest(TestCase):
    def setUp(self):
        # o cache do rate limit persiste entre métodos de uma mesma classe de
        # teste (não é limpo pelo rollback de transação do TestCase) — sem
        # isto, os vários POSTs desta classe se acumulariam contra o limite
        # de 5/h e passariam a falhar por "muitas tentativas".
        from django.core.cache import cache

        cache.clear()

    def _valid_payload(self, **overrides):
        payload = {
            "name": "Espaço Beleza",
            "email": "dona@espacobeleza.com",
            "password1": "senhaSuperSegura123",
            "password2": "senhaSuperSegura123",
            "theme": "salao",
            "accept_terms": "on",
        }
        payload.update(overrides)
        return payload

    def test_get_renders_form(self):
        response = self.client.get("/cadastrar/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nome do estabelecimento")
        self.assertContains(response, "Termos de Uso")

    def test_signup_creates_tenant_and_logs_in(self):
        response = self.client.post("/cadastrar/", self._valid_payload())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/painel/")
        tenant = Tenant.objects.get(name="Espaço Beleza")
        user = User.objects.get(email="dona@espacobeleza.com")
        self.assertEqual(user.tenant, tenant)
        self.assertEqual(user.role, User.Role.TENANT_ADMIN)
        # já autenticado após o cadastro
        response = self.client.get("/painel/servicos/")
        self.assertEqual(response.status_code, 200)

    def test_signup_creates_unverified_primary_email_and_sends_confirmation(self):
        """Regressão: `EmailAddress.objects.add_email()` não marca `primary`
        sozinho (ver allauth/account/managers.py) — sem o `set_as_primary`
        explícito em `signup_view`, o e-mail ficava órfão sem primary."""
        from django.core import mail

        self.client.post("/cadastrar/", self._valid_payload())
        user = User.objects.get(email="dona@espacobeleza.com")
        email_address = EmailAddress.objects.get(user=user, email=user.email)
        self.assertTrue(email_address.primary)
        self.assertFalse(email_address.verified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Confirme seu e-mail", mail.outbox[0].subject)

    def test_password_mismatch_rejected(self):
        response = self.client.post(
            "/cadastrar/", self._valid_payload(password2="outrasenha123")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tenant.objects.filter(name="Espaço Beleza").exists())

    def test_signup_persists_chosen_theme(self):
        self.client.post("/cadastrar/", self._valid_payload(theme="barbearia"))
        tenant = Tenant.objects.get(name="Espaço Beleza")
        self.assertEqual(tenant.theme, "barbearia")

    @override_settings(ALLOWED_HOSTS=["barbearia.zellup.com.br"])
    def test_subdomain_forces_theme_and_hides_radio(self):
        """Decisão do usuário em 2026-08-03: no subdomínio dedicado, o
        cadastro nem pergunta o tema — o servidor decide pelo host, mesmo
        que o POST venha com outro valor (campo oculto adulterado, cache
        antigo etc.)."""
        response = self.client.get(
            "/cadastrar/", HTTP_HOST="barbearia.zellup.com.br"
        )
        self.assertNotContains(response, 'value="salao"')
        self.assertContains(response, 'value="barbearia"')
        response = self.client.post(
            "/cadastrar/",
            self._valid_payload(theme="salao"),
            HTTP_HOST="barbearia.zellup.com.br",
        )
        self.assertEqual(response.status_code, 302)
        tenant = Tenant.objects.get(name="Espaço Beleza")
        self.assertEqual(tenant.theme, "barbearia")

    def test_weak_password_rejected(self):
        response = self.client.post(
            "/cadastrar/", self._valid_payload(password1="12345678", password2="12345678")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tenant.objects.filter(name="Espaço Beleza").exists())

    def test_terms_not_accepted_rejected(self):
        response = self.client.post("/cadastrar/", self._valid_payload(accept_terms=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tenant.objects.filter(name="Espaço Beleza").exists())

    def test_duplicate_email_shows_error_not_second_tenant(self):
        self.client.post("/cadastrar/", self._valid_payload())
        self.client.logout()
        response = self.client.post(
            "/cadastrar/", self._valid_payload(name="Outro Salão")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Tenant.objects.filter(name="Outro Salão").count(), 0)

    def test_terms_and_privacy_pages_reachable(self):
        self.assertEqual(self.client.get("/termos/").status_code, 200)
        self.assertEqual(self.client.get("/privacidade/").status_code, 200)


class LandingViewTest(TestCase):
    """Domínio raiz (`/`) — landing com os 2 caminhos; nos subdomínios
    dedicados, a raiz redireciona direto pro login/cadastro daquele tema
    (decisão do usuário em 2026-08-03)."""

    def test_root_domain_shows_landing_with_both_links(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sou Barbearia")
        self.assertContains(response, "Sou Salão de Beleza")
        self.assertContains(response, "barbearia.testserver/painel/login/")
        self.assertContains(response, "salao.testserver/painel/login/")

    @override_settings(ALLOWED_HOSTS=["barbearia.zellup.com.br"])
    def test_barbearia_subdomain_root_redirects_to_login(self):
        response = self.client.get("/", HTTP_HOST="barbearia.zellup.com.br")
        self.assertRedirects(
            response, "/painel/login/", fetch_redirect_response=False
        )

    @override_settings(ALLOWED_HOSTS=["salao.zellup.com.br"])
    def test_salao_subdomain_root_redirects_to_login(self):
        response = self.client.get("/", HTTP_HOST="salao.zellup.com.br")
        self.assertRedirects(
            response, "/painel/login/", fetch_redirect_response=False
        )


@override_settings(CACHES=LOCMEM_CACHE)
class SignUpRateLimitTest(TestCase):
    """Isolada em sua própria classe: o cache do rate limit (mesmo LocMem)
    persiste entre métodos de uma mesma classe de teste, então qualquer outro
    teste de /cadastrar/ nesta classe contaria para o mesmo limite de 5/h."""

    def _valid_payload(self, **overrides):
        payload = {
            "name": "Espaço Beleza",
            "email": "dona@espacobeleza.com",
            "password1": "senhaSuperSegura123",
            "password2": "senhaSuperSegura123",
            "theme": "salao",
            "accept_terms": "on",
        }
        payload.update(overrides)
        return payload

    def test_rate_limited_after_five_signups_per_hour(self):
        for i in range(5):
            response = self.client.post(
                "/cadastrar/",
                self._valid_payload(
                    name=f"Salão {i}", email=f"dona{i}@salao.com"
                ),
            )
            self.assertEqual(response.status_code, 302, f"tentativa {i + 1} deveria ser aceita")
            self.client.logout()

        response = self.client.post(
            "/cadastrar/", self._valid_payload(name="Salão 6", email="dona6@salao.com")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tenant.objects.filter(name="Salão 6").exists())


class DeleteTenantAccountDomainTest(TestCase):
    """Confirma empiricamente que a exclusão em cascata funciona mesmo com
    toda a cadeia protegida por PROTECT (Appointment, Commission,
    CashTransaction) — ver apps/tenants/services.py::delete_tenant_account."""

    def _build_full_chain(self, slug):
        import datetime

        from apps.employees.services import create_employee, link_service, set_working_hours
        from apps.scheduling.services import complete_appointment, create_appointment
        from apps.services.services import create_service

        tenant = Tenant.objects.create(name=f"Salão {slug}", slug=slug)
        employee = create_employee(
            tenant=tenant, full_name="Func", email=f"func@{slug}.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40"),
        )
        service = create_service(
            tenant=tenant, name="Corte", duration_minutes=60, price=Decimal("100")
        )
        link_service(employee, service)
        set_working_hours(
            employee,
            [
                {"weekday": wd, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}
                for wd in range(7)
            ],
        )
        client_ = Client.objects.create(tenant=tenant, phone="11900000000", name="Cliente")
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        appointment = create_appointment(
            tenant=tenant, client=client_, employee=employee, service=service,
            date=tomorrow, start_time=datetime.time(9, 0),
        )
        complete_appointment(appointment=appointment, payment_method="cash", created_by=employee.user)

        # Produto com lote/validade (RF44) — ProductBatch/StockMovementBatch
        # também protegem Product/StockMovement e precisam sair na exclusão.
        from apps.inventory import services as inventory_ops

        batch_product = inventory_ops.create_product(
            tenant=tenant, name="Esmalte", unit="un", cost_price=Decimal("5"),
            sale_price=Decimal("10"), min_stock_alert=Decimal("1"), tracks_batches=True,
        )
        inventory_ops.register_stock_movement(
            tenant=tenant, product=batch_product, movement_type="in", quantity=Decimal("5"),
            unit_price=Decimal("5"), reason="purchase", created_by=employee.user,
            batch_number="L1", expiry_date=tomorrow,
        )

        # Contagem de inventário físico (RF46) — PhysicalInventoryCountItem
        # também protege Product e precisa sair na exclusão.
        inventory_ops.start_inventory_count(tenant=tenant, created_by=employee.user)

        return tenant

    def test_deletes_tenant_with_full_dependency_chain(self):
        tenant = self._build_full_chain("salao-delete-a")
        tenant_id = tenant.pk
        delete_tenant_account(tenant)
        self.assertFalse(Tenant.objects.filter(pk=tenant_id).exists())

    def test_removes_all_users_of_the_tenant(self):
        tenant = self._build_full_chain("salao-delete-b")
        from apps.accounts.models import User as AccountUser

        emails = list(AccountUser.objects.filter(tenant=tenant).values_list("email", flat=True))
        self.assertTrue(emails)
        delete_tenant_account(tenant)
        self.assertFalse(AccountUser.objects.filter(email__in=emails).exists())

    def test_does_not_affect_other_tenants(self):
        tenant_a = self._build_full_chain("salao-delete-c")
        tenant_b = self._build_full_chain("salao-delete-d")
        delete_tenant_account(tenant_a)
        self.assertTrue(Tenant.objects.filter(pk=tenant_b.pk).exists())
        from apps.services.models import Service

        self.assertTrue(Service.objects.filter(tenant=tenant_b).exists())


class DeleteAccountPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.admin = User.objects.create_user(
            email="dona@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=cls.tenant,
        )

    def test_login_required(self):
        response = self.client.get("/painel/configuracoes/excluir-conta/confirmar/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=self.tenant,
        )
        self.client.force_login(employee)
        response = self.client.get("/painel/configuracoes/excluir-conta/confirmar/")
        self.assertEqual(response.status_code, 403)

    def test_confirm_modal_has_no_native_confirm(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/configuracoes/excluir-conta/confirmar/")
        self.assertContains(response, "EXCLUIR MINHACONTA")
        self.assertNotContains(response, "hx-confirm")

    def test_wrong_confirmation_text_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/configuracoes/excluir-conta/", {"confirmation": "excluir"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Retarget"), "#modal-slot")
        self.assertTrue(Tenant.objects.filter(pk=self.tenant.pk).exists())

    def test_correct_confirmation_deletes_account_and_logs_out(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/configuracoes/excluir-conta/", {"confirmation": "EXCLUIR MINHACONTA"}
        )
        self.assertEqual(response.headers.get("HX-Redirect"), "/painel/conta-excluida/")
        self.assertFalse(Tenant.objects.filter(pk=self.tenant.pk).exists())
        # sessão encerrada — próxima página do painel exige login de novo
        response = self.client.get("/painel/servicos/")
        self.assertEqual(response.status_code, 302)


def _make_test_image(width, height, image_format="JPEG", mode="RGB", color=(200, 100, 50)):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new(mode, (width, height), color).save(buffer, format=image_format)
    buffer.seek(0)
    content_type = f"image/{image_format.lower()}"
    return SimpleUploadedFile(f"foto.{image_format.lower()}", buffer.read(), content_type=content_type)


class TenantImageCompressionTest(TestCase):
    """Upload de imagem sem limite nenhum hoje (regra 2026-08-05) — logo/
    capa/fundo/foto do responsável precisam ser redimensionados e
    recomprimidos no `save()` do model, sem depender de cada form/view
    lembrar de fazer isso."""

    def test_large_logo_is_resized_on_save(self):
        from PIL import Image

        tenant = Tenant.objects.create(name="Salão Foto", slug="salao-foto")
        tenant.logo = _make_test_image(2400, 1800, "JPEG")
        tenant.save()
        tenant.refresh_from_db()
        with Image.open(tenant.logo) as saved:
            self.assertLessEqual(max(saved.size), 1600)

    def test_small_image_is_not_upscaled(self):
        from PIL import Image

        tenant = Tenant.objects.create(name="Salão Foto Pequena", slug="salao-foto-pequena")
        tenant.logo = _make_test_image(400, 300, "JPEG")
        tenant.save()
        tenant.refresh_from_db()
        with Image.open(tenant.logo) as saved:
            self.assertEqual(saved.size, (400, 300))

    def test_png_transparency_preserved(self):
        from PIL import Image

        tenant = Tenant.objects.create(name="Salão PNG", slug="salao-png")
        tenant.logo = _make_test_image(2000, 2000, "PNG", mode="RGBA", color=(0, 0, 0, 0))
        tenant.save()
        tenant.refresh_from_db()
        with Image.open(tenant.logo) as saved:
            self.assertEqual(saved.format, "PNG")
            self.assertEqual(saved.mode, "RGBA")
            self.assertLessEqual(max(saved.size), 1600)

    def test_resaving_without_changing_image_does_not_reprocess(self):
        tenant = Tenant.objects.create(name="Salão Resave", slug="salao-resave")
        tenant.logo = _make_test_image(2400, 1800, "JPEG")
        tenant.save()
        original_name = tenant.logo.name

        tenant.name = "Salão Resave Renomeado"
        tenant.save()

        self.assertEqual(tenant.logo.name, original_name)


class SupportModalViewTest(TestCase):
    """Botão "Suporte" — acessível a tenant_admin E funcionário (decisão do
    usuário em 2026-08-09: não é bloqueado por RF30, é justamente quando a
    assinatura está com problema que mais se precisa de ajuda)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.admin = User.objects.create_user(
            email="dona@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=cls.tenant,
        )

    def test_login_required(self):
        response = self.client.get("/painel/suporte/")
        self.assertEqual(response.status_code, 302)

    def test_tenant_admin_sees_configured_number(self):
        from apps.billing.services import update_platform_settings

        update_platform_settings(support_whatsapp="34999998888")
        self.client.force_login(self.admin)
        response = self.client.get("/painel/suporte/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "5534999998888")
        self.assertContains(response, "Abrir WhatsApp")

    def test_employee_can_also_reach_support(self):
        from apps.employees.services import create_employee

        employee = create_employee(
            tenant=self.tenant, full_name="Ana", email="ana@salao-a.com",
            password="Senha@123", default_commission_type="percentage",
            default_commission_value=Decimal("40"),
        )
        self.client.force_login(employee.user)
        response = self.client.get("/painel/suporte/")
        self.assertEqual(response.status_code, 200)

    def test_graceful_message_when_number_not_configured(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/suporte/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ainda não foi configurado")
        self.assertNotContains(response, "Abrir WhatsApp")

    def test_message_includes_tenant_name_and_category_options(self):
        from apps.billing.services import update_platform_settings

        update_platform_settings(support_whatsapp="34999998888")
        self.client.force_login(self.admin)
        response = self.client.get("/painel/suporte/")
        self.assertContains(response, "Salão A")
        self.assertContains(response, "Erro no sistema")
        self.assertContains(response, "Cobrança/assinatura")
