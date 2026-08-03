from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings

from apps.tenants.models import Tenant

User = get_user_model()


class UserModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")

    def test_create_user_logs_in_by_email(self):
        user = User.objects.create_user(
            email="func@salao-a.com",
            password="segredo123",
            role=User.Role.EMPLOYEE,
            tenant=self.tenant,
        )
        self.assertEqual(User.USERNAME_FIELD, "email")
        self.assertTrue(user.check_password("segredo123"))
        self.assertNotEqual(user.password, "segredo123")  # nunca texto puro

    def test_create_superuser_has_no_tenant(self):
        admin = User.objects.create_superuser(
            email="root@plataforma.com", password="segredo123"
        )
        self.assertEqual(admin.role, User.Role.SUPERADMIN)
        self.assertIsNone(admin.tenant)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)

    def test_email_is_unique(self):
        User.objects.create_user(
            email="dona@salao-a.com",
            password="x",
            role=User.Role.TENANT_ADMIN,
            tenant=self.tenant,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(
                email="dona@salao-a.com",
                password="y",
                role=User.Role.TENANT_ADMIN,
                tenant=self.tenant,
            )

    def test_employee_without_tenant_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(
                email="orfao@nada.com",
                password="x",
                role=User.Role.EMPLOYEE,
                tenant=None,
            )

    def test_superadmin_with_tenant_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(
                email="root-errado@plataforma.com",
                password="x",
                role=User.Role.SUPERADMIN,
                tenant=self.tenant,
            )


class EmployeePanelIsolationTest(TestCase):
    """Reforça 02-ARQUITETURA.md §5: funcionário nunca acessa caixa geral,
    estoque, agenda geral ou outros dados administrativos do tenant."""

    @classmethod
    def setUpTestData(cls):
        from apps.employees.services import create_employee

        cls.tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        cls.employee = create_employee(
            tenant=cls.tenant,
            full_name="Ana Silva",
            email="ana@salao-a.com",
            password="Senha@123",
            default_commission_type="percentage",
            default_commission_value=Decimal("40"),
        )

    def test_employee_cannot_access_general_cash_panel(self):
        self.client.force_login(self.employee.user)
        response = self.client.get("/painel/caixa/")
        self.assertEqual(response.status_code, 403)

    def test_employee_cannot_access_inventory_panel(self):
        self.client.force_login(self.employee.user)
        response = self.client.get("/painel/estoque/")
        self.assertEqual(response.status_code, 403)

    def test_employee_cannot_access_general_agenda_panel(self):
        self.client.force_login(self.employee.user)
        response = self.client.get("/painel/agenda/")
        self.assertEqual(response.status_code, 403)

    def test_employee_cannot_access_employees_panel(self):
        self.client.force_login(self.employee.user)
        response = self.client.get("/painel/funcionarios/")
        self.assertEqual(response.status_code, 403)

    def test_employee_cannot_access_settings_panel(self):
        self.client.force_login(self.employee.user)
        response = self.client.get("/painel/configuracoes/")
        self.assertEqual(response.status_code, 403)

    def test_painel_home_redirects_employee_to_my_agenda(self):
        self.client.force_login(self.employee.user)
        response = self.client.get("/painel/")
        self.assertRedirects(response, "/painel/minha-agenda/")

    def test_painel_home_redirects_admin_to_services(self):
        admin = User.objects.create_user(
            email="dona@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=self.tenant,
        )
        self.client.force_login(admin)
        response = self.client.get("/painel/")
        self.assertRedirects(response, "/painel/servicos/")

    def test_tenant_admin_without_employee_profile_cannot_access_my_agenda(self):
        admin = User.objects.create_user(
            email="dona2@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=self.tenant,
        )
        self.client.force_login(admin)
        response = self.client.get("/painel/minha-agenda/")
        self.assertEqual(response.status_code, 403)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get("/painel/minha-agenda/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/painel/login/", response.url)


class ThemeConfirmationGateTest(TestCase):
    """Decisão do usuário em 2026-08-02: tenant criado via Google
    (`theme_confirmed=False`) é redirecionado pra `painel/escolher-tema/`
    em qualquer view de `tenant_admin_required`, até confirmar o tema —
    ver `apps.accounts.decorators.tenant_admin_required`."""

    def test_unconfirmed_theme_redirects_to_choose_theme(self):
        tenant = Tenant.objects.create(name="Salão A", slug="salao-a", theme_confirmed=False)
        admin = User.objects.create_user(
            email="dona@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=tenant,
        )
        self.client.force_login(admin)
        response = self.client.get("/painel/servicos/")
        self.assertRedirects(response, "/painel/escolher-tema/")

    def test_confirmed_theme_accesses_normally(self):
        tenant = Tenant.objects.create(name="Salão A", slug="salao-a", theme_confirmed=True)
        admin = User.objects.create_user(
            email="dona@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=tenant,
        )
        self.client.force_login(admin)
        response = self.client.get("/painel/servicos/")
        self.assertEqual(response.status_code, 200)

    def test_employee_not_gated_by_tenant_theme_confirmation(self):
        """Só o admin escolhe o tema — funcionário não é bloqueado por isso."""
        from apps.employees.services import create_employee

        tenant = Tenant.objects.create(name="Salão A", slug="salao-a", theme_confirmed=False)
        employee = create_employee(
            tenant=tenant,
            full_name="Ana Silva",
            email="ana@salao-a.com",
            password="Senha@123",
            default_commission_type="percentage",
            default_commission_value=Decimal("40"),
        )
        self.client.force_login(employee.user)
        response = self.client.get("/painel/minha-agenda/")
        self.assertEqual(response.status_code, 200)


def _social_request(host=None):
    """Request "de mentira" com session/messages — o que `SocialLogin.save`/
    `.connect` do allauth espera encontrar disponível. `host` simula o
    subdomínio (`barbearia.zellup.com.br`/`salao.zellup.com.br`) usado pra
    detectar o tema automaticamente (decisão do usuário em 2026-08-03)."""
    extra = {"SERVER_NAME": host} if host else {}
    request = RequestFactory().get("/accounts/google/login/callback/", **extra)
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    MessageMiddleware(lambda r: None).process_request(request)
    return request


def _make_sociallogin(email, name="Julia Nova", uid="google-uid-1"):
    from allauth.socialaccount.models import SocialAccount, SocialLogin

    social_user = User(email=email)
    account = SocialAccount(
        provider="google", uid=uid, extra_data={"email": email, "name": name}
    )
    return SocialLogin(user=social_user, account=account)


class GoogleSocialLoginAdapterTest(TestCase):
    """Login com Google (decisão do usuário em 2026-07-30): e-mail existente
    vincula a conta; e-mail novo cria tenant + user automaticamente — ver
    apps/accounts/adapters.py."""

    def test_pre_social_login_links_existing_user_by_email(self):
        from apps.accounts.adapters import ZellupSocialAccountAdapter

        tenant = Tenant.objects.create(name="Salão A", slug="salao-a")
        existing = User.objects.create_user(
            email="dona@salao-a.com", password="x", role=User.Role.TENANT_ADMIN, tenant=tenant,
        )
        sociallogin = _make_sociallogin(email="dona@salao-a.com")
        ZellupSocialAccountAdapter().pre_social_login(_social_request(), sociallogin)

        self.assertTrue(sociallogin.is_existing)
        self.assertEqual(sociallogin.user.pk, existing.pk)

    def test_pre_social_login_ignores_email_with_no_match(self):
        from apps.accounts.adapters import ZellupSocialAccountAdapter

        sociallogin = _make_sociallogin(email="ninguem@gmail.com")
        ZellupSocialAccountAdapter().pre_social_login(_social_request(), sociallogin)

        self.assertFalse(sociallogin.is_existing)

    def test_save_user_creates_tenant_and_admin_for_new_email(self):
        from apps.accounts.adapters import ZellupSocialAccountAdapter
        from apps.billing.models import Subscription
        from apps.tenants.models import TenantBusinessHours

        sociallogin = _make_sociallogin(email="nova@gmail.com", name="Julia Nova")
        user = ZellupSocialAccountAdapter().save_user(_social_request(), sociallogin)

        self.assertEqual(user.email, "nova@gmail.com")
        self.assertEqual(user.role, User.Role.TENANT_ADMIN)
        self.assertFalse(user.has_usable_password())  # login só via Google
        self.assertIsNotNone(user.tenant)
        self.assertEqual(user.tenant.name, "Salão de Julia Nova")
        # mesmo caminho de register_tenant: jornada padrão + assinatura em teste
        self.assertEqual(
            TenantBusinessHours.objects.filter(tenant=user.tenant).count(), 7
        )
        self.assertTrue(Subscription.objects.filter(tenant=user.tenant).exists())
        # Decisão do usuário em 2026-08-02: cadastro via Google não passa
        # tema, nasce não confirmado — força a tela `painel/escolher-tema/`.
        self.assertFalse(user.tenant.theme_confirmed)

    @override_settings(ALLOWED_HOSTS=["barbearia.zellup.com.br"])
    def test_save_user_on_barbearia_subdomain_confirms_theme_automatically(self):
        """Decisão do usuário em 2026-08-03: login via Google no subdomínio
        dedicado já sabe o tema — pula `painel/escolher-tema/`."""
        from apps.accounts.adapters import ZellupSocialAccountAdapter

        sociallogin = _make_sociallogin(email="beto@gmail.com", name="Beto")
        user = ZellupSocialAccountAdapter().save_user(
            _social_request(host="barbearia.zellup.com.br"), sociallogin
        )

        self.assertEqual(user.tenant.theme, "barbearia")
        self.assertTrue(user.tenant.theme_confirmed)
        self.assertEqual(user.tenant.name, "Barbearia de Beto")

    @override_settings(ALLOWED_HOSTS=["salao.zellup.com.br"])
    def test_save_user_on_salao_subdomain_confirms_theme_automatically(self):
        from apps.accounts.adapters import ZellupSocialAccountAdapter

        sociallogin = _make_sociallogin(email="clara@gmail.com", name="Clara")
        user = ZellupSocialAccountAdapter().save_user(
            _social_request(host="salao.zellup.com.br"), sociallogin
        )

        self.assertEqual(user.tenant.theme, "salao")
        self.assertTrue(user.tenant.theme_confirmed)
        self.assertEqual(user.tenant.name, "Salão de Clara")

    def test_full_flow_new_email_then_relogin_links_same_user(self):
        """Segunda vez que a MESMA conta Google loga, `sociallogin.is_existing`
        já vem True (allauth resolve pelo par provider+uid persistido) — não
        deve tentar criar um segundo tenant."""
        from allauth.socialaccount.models import SocialAccount

        from apps.accounts.adapters import ZellupSocialAccountAdapter

        adapter = ZellupSocialAccountAdapter()
        first_login = _make_sociallogin(email="dupla@gmail.com", uid="google-uid-9")
        user = adapter.save_user(_social_request(), first_login)

        self.assertEqual(SocialAccount.objects.filter(uid="google-uid-9").count(), 1)
        self.assertEqual(User.objects.filter(email="dupla@gmail.com").count(), 1)
        self.assertEqual(SocialAccount.objects.get(uid="google-uid-9").user_id, user.id)


class ZellupLoginViewTest(TestCase):
    """`/painel/login/` é a mesma URL em todo host — o subdomínio decide
    qual das 2 telas (`templates/painel/login.html`) renderiza (decisão do
    usuário em 2026-08-03)."""

    @override_settings(ALLOWED_HOSTS=["barbearia.zellup.com.br"])
    def test_barbearia_subdomain_renders_dark_theme(self):
        response = self.client.get(
            "/painel/login/", HTTP_HOST="barbearia.zellup.com.br"
        )
        self.assertContains(response, 'class="dark"')
        self.assertContains(response, "Bem-vindo de volta")

    @override_settings(ALLOWED_HOSTS=["salao.zellup.com.br"])
    def test_salao_subdomain_renders_light_theme(self):
        response = self.client.get(
            "/painel/login/", HTTP_HOST="salao.zellup.com.br"
        )
        self.assertContains(response, 'class="light"')
        self.assertContains(response, "A arte de agendar com elegância e precisão.")

    def test_plain_host_defaults_to_light_theme(self):
        response = self.client.get("/painel/login/")
        self.assertContains(response, 'class="light"')
