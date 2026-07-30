import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from apps.tenants.models import Tenant
from apps.tenants.services import register_tenant
from apps.tenants.tests import IsolationProbe

from . import services as billing_ops
from .models import Plan, Subscription, SubscriptionStatus

User = get_user_model()


def setUpModule():
    """`apps.billing.tests` roda em ordem alfabética ANTES de
    `apps.tenants.tests` — como este arquivo também exercita
    `delete_tenant_account` (ver SubscriberPanelTest), precisamos garantir
    que a tabela de `IsolationProbe` já exista antes do cascade de
    `tenant.delete()`, senão estoura "relation does not exist". O
    `setUpModule` de `apps.tenants.tests` é idempotente e só faz a limpeza
    final quando roda depois."""
    if IsolationProbe._meta.db_table not in connection.introspection.table_names():
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(IsolationProbe)


def make_tenant_with_admin(slug):
    tenant = Tenant.objects.create(name=f"Salão {slug}", slug=slug)
    admin = User.objects.create_user(
        email=f"admin@{slug}.com", password="x", role=User.Role.TENANT_ADMIN, tenant=tenant
    )
    # Diferente de register_tenant (fluxo real de /cadastrar/), criar o
    # Tenant direto no teste não passa pelo hook que gera a Subscription —
    # replicamos aqui pra exercitar as views que dependem de
    # tenant.subscription existir.
    billing_ops.create_subscription_for_tenant(tenant)
    return tenant, admin


def make_superadmin(email="root@zelo.local"):
    return User.objects.create_user(
        email=email, password="x", role=User.Role.SUPERADMIN, tenant=None,
        is_staff=True, is_superuser=True,
    )


class SubscriptionAutoCreationTest(TestCase):
    def test_register_tenant_creates_trialing_subscription_without_plan(self):
        tenant, _ = register_tenant(name="Salão Novo", email="dono@salao-novo.com", password="Senha@123")
        subscription = Subscription.objects.get(tenant=tenant)
        self.assertEqual(subscription.status, SubscriptionStatus.TRIALING)
        self.assertIsNone(subscription.plan)


class PlanDomainTest(TestCase):
    def test_create_plan(self):
        plan = billing_ops.create_plan(name="Essencial", price=Decimal("99.90"), description="Básico")
        self.assertTrue(plan.is_active)
        self.assertEqual(plan.price, Decimal("99.90"))

    def test_update_plan(self):
        plan = billing_ops.create_plan(name="Essencial", price=Decimal("99.90"))
        billing_ops.update_plan(
            plan, name="Essencial Plus", price=Decimal("149.90"), description="Atualizado",
            is_active=False, order=2,
        )
        plan.refresh_from_db()
        self.assertEqual(plan.name, "Essencial Plus")
        self.assertFalse(plan.is_active)


class SubscriptionChangeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.plan = Plan.objects.create(name="Pro", price=Decimal("199.90"))

    def test_change_plan_and_status(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        billing_ops.change_subscription_plan(subscription, self.plan)
        billing_ops.change_subscription_status(subscription, SubscriptionStatus.ACTIVE)
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan, self.plan)
        self.assertEqual(subscription.status, SubscriptionStatus.ACTIVE)

    def test_change_plan_sets_30_day_period(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        billing_ops.change_subscription_plan(subscription, self.plan)
        subscription.refresh_from_db()
        today = timezone.localdate()
        self.assertEqual(subscription.current_period_start, today)
        self.assertEqual(subscription.current_period_end, today + datetime.timedelta(days=30))

    def test_removing_plan_clears_period(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        billing_ops.change_subscription_plan(subscription, self.plan)
        billing_ops.change_subscription_plan(subscription, None)
        subscription.refresh_from_db()
        self.assertIsNone(subscription.plan)
        self.assertIsNone(subscription.current_period_start)
        self.assertIsNone(subscription.current_period_end)

    def test_manual_period_edit(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        start = datetime.date(2026, 1, 1)
        end = datetime.date(2026, 2, 15)
        billing_ops.update_subscription_period(
            subscription, current_period_start=start, current_period_end=end
        )
        subscription.refresh_from_db()
        self.assertEqual(subscription.current_period_start, start)
        self.assertEqual(subscription.current_period_end, end)

    def test_manual_period_edit_rejects_end_before_start(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        with self.assertRaises(ValidationError):
            billing_ops.update_subscription_period(
                subscription,
                current_period_start=datetime.date(2026, 2, 1),
                current_period_end=datetime.date(2026, 1, 1),
            )

    def test_recurring_subscription_blocks_manual_plan_and_period_changes(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.asaas_subscription_id = "sub_123"
        subscription.save(update_fields=["asaas_subscription_id"])

        with self.assertRaises(ValidationError):
            billing_ops.change_subscription_plan(subscription, self.plan)
        with self.assertRaises(ValidationError):
            billing_ops.update_subscription_period(
                subscription,
                current_period_start=datetime.date(2026, 1, 1),
                current_period_end=datetime.date(2026, 1, 31),
            )


class PlataformaAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee_user = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )
        cls.superadmin = make_superadmin()

    def test_login_required(self):
        response = self.client.get("/plataforma/")
        self.assertEqual(response.status_code, 302)

    def test_tenant_admin_forbidden(self):
        self.client.force_login(self.admin)
        response = self.client.get("/plataforma/")
        self.assertEqual(response.status_code, 403)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee_user)
        response = self.client.get("/plataforma/")
        self.assertEqual(response.status_code, 403)

    def test_superadmin_can_access_dashboard(self):
        self.client.force_login(self.superadmin)
        response = self.client.get("/plataforma/")
        self.assertEqual(response.status_code, 200)

    def test_painel_home_redirects_superadmin_to_plataforma(self):
        self.client.force_login(self.superadmin)
        response = self.client.get("/painel/")
        self.assertRedirects(response, "/plataforma/")


class PlanPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superadmin = make_superadmin()

    def test_create_plan_via_panel(self):
        self.client.force_login(self.superadmin)
        response = self.client.post(
            "/plataforma/planos/novo/",
            {"name": "Essencial", "price": "99,90", "description": "", "order": "0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Plan.objects.filter(name="Essencial").exists())

    def test_toggle_plan(self):
        plan = Plan.objects.create(name="Pro", price=Decimal("199.90"))
        self.client.force_login(self.superadmin)
        self.client.post(f"/plataforma/planos/{plan.pk}/toggle/")
        plan.refresh_from_db()
        self.assertFalse(plan.is_active)


class SubscriberPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.superadmin = make_superadmin()

    def test_subscriber_list_shows_tenant(self):
        self.client.force_login(self.superadmin)
        response = self.client.get("/plataforma/assinantes/")
        self.assertContains(response, "Salão salao-a")

    def test_filter_by_status(self):
        self.client.force_login(self.superadmin)
        response = self.client.get("/plataforma/assinantes/", {"status": "active"})
        self.assertNotContains(response, "Salão salao-a")

    def test_suspend_and_reactivate_tenant(self):
        self.client.force_login(self.superadmin)
        self.client.post(f"/plataforma/assinantes/{self.tenant.id}/acesso/")
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.is_active)
        self.client.post(f"/plataforma/assinantes/{self.tenant.id}/acesso/")
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.is_active)

    def test_change_status_and_plan(self):
        plan = Plan.objects.create(name="Pro", price=Decimal("199.90"))
        self.client.force_login(self.superadmin)
        self.client.post(
            f"/plataforma/assinantes/{self.tenant.id}/status/", {"status": "active"}
        )
        self.client.post(f"/plataforma/assinantes/{self.tenant.id}/plano/", {"plan": plan.pk})
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(subscription.status, "active")
        self.assertEqual(subscription.plan, plan)

    def test_change_period_via_panel(self):
        self.client.force_login(self.superadmin)
        response = self.client.post(
            f"/plataforma/assinantes/{self.tenant.id}/periodo/",
            {"current_period_start": "2026-01-01", "current_period_end": "2026-01-31"},
        )
        self.assertRedirects(
            response, f"/plataforma/assinantes/{self.tenant.id}/"
        )
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(subscription.current_period_start, datetime.date(2026, 1, 1))
        self.assertEqual(subscription.current_period_end, datetime.date(2026, 1, 31))

    def test_change_period_blocked_for_recurring_subscription(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.asaas_subscription_id = "sub_123"
        subscription.current_period_end = datetime.date(2026, 5, 1)
        subscription.save(update_fields=["asaas_subscription_id", "current_period_end"])

        self.client.force_login(self.superadmin)
        self.client.post(
            f"/plataforma/assinantes/{self.tenant.id}/periodo/",
            {"current_period_start": "2026-01-01", "current_period_end": "2026-01-31"},
        )
        subscription.refresh_from_db()
        self.assertEqual(subscription.current_period_end, datetime.date(2026, 5, 1))

    def test_delete_requires_exact_slug(self):
        self.client.force_login(self.superadmin)
        response = self.client.post(
            f"/plataforma/assinantes/{self.tenant.id}/excluir/", {"confirmation": "errado"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Tenant.objects.filter(pk=self.tenant.pk).exists())

    def test_delete_with_correct_slug_removes_tenant(self):
        self.client.force_login(self.superadmin)
        response = self.client.post(
            f"/plataforma/assinantes/{self.tenant.id}/excluir/", {"confirmation": self.tenant.slug}
        )
        self.assertEqual(response.headers.get("HX-Redirect"), "/plataforma/assinantes/")
        self.assertFalse(Tenant.objects.filter(pk=self.tenant.pk).exists())
