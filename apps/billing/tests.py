import datetime
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tenants.models import Tenant, validate_cpf_cnpj
from apps.tenants.services import register_tenant
from apps.tenants.tests import IsolationProbe

from . import asaas_client
from . import services as billing_ops
from .models import AsaasWebhookEvent, Plan, Subscription, SubscriptionStatus

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


def make_superadmin(email="root@zellup.local"):
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
        plan = billing_ops.create_plan(name="Plano Teste", price=Decimal("99.90"), description="Básico")
        self.assertTrue(plan.is_active)
        self.assertEqual(plan.price, Decimal("99.90"))

    def test_update_plan(self):
        plan = billing_ops.create_plan(name="Plano Teste", price=Decimal("99.90"))
        billing_ops.update_plan(
            plan, name="Plano Teste Plus", price=Decimal("149.90"), description="Atualizado",
            is_active=False, order=2,
        )
        plan.refresh_from_db()
        self.assertEqual(plan.name, "Plano Teste Plus")
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


class PlanDowngradeEmployeeLimitTest(TestCase):
    """Trava de downgrade (decisão do usuário em 2026-08-07): não deixa o
    tenant assinar um plano com `max_employees` menor do que a quantidade de
    funcionários ativos que ele já tem — nem pelo self-service
    (`select_plan`, antes do checkout) nem pelo override do superadmin
    (`change_subscription_plan`). O plano atual não muda quando barrado."""

    @classmethod
    def setUpTestData(cls):
        from apps.employees.services import create_employee

        cls.tenant, cls.admin = make_tenant_with_admin("salao-downgrade")
        cls.superadmin = make_superadmin(email="root-downgrade@zellup.local")
        cls.studio = Plan.objects.get(name="Studio")  # max_employees=6
        cls.profissional = Plan.objects.get(name="Profissional")  # max_employees=3
        for i in range(4):
            create_employee(
                tenant=cls.tenant,
                full_name=f"Funcionário {i}",
                email=f"func{i}@salao-downgrade.com",
                password="Senha@123",
                default_commission_type="percentage",
                default_commission_value=Decimal("40"),
            )

    def test_select_plan_blocked_when_over_new_limit(self):
        """4 funcionários ativos, tentando assinar o Profissional (limite 3)."""
        subscription = Subscription.objects.get(tenant=self.tenant)
        with self.assertRaises(ValidationError) as ctx:
            billing_ops.select_plan(subscription, self.profissional)
        message = " ".join(ctx.exception.messages)
        self.assertIn("4 funcionários", message)
        self.assertIn("Profissional", message)
        self.assertIn("3", message)
        subscription.refresh_from_db()
        self.assertIsNone(subscription.plan)

    def test_select_plan_allowed_when_within_new_limit(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        billing_ops.select_plan(subscription, self.studio)
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan, self.studio)

    def test_change_subscription_plan_blocked_when_over_new_limit(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        with self.assertRaises(ValidationError):
            billing_ops.change_subscription_plan(subscription, self.profissional)
        subscription.refresh_from_db()
        self.assertIsNone(subscription.plan)

    def test_view_shows_message_and_keeps_current_plan(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.studio
        subscription.status = "trialing"
        subscription.save(update_fields=["plan", "status"])
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/plano/assinar/{self.profissional.pk}/", follow=True
        )
        self.assertContains(response, "4 funcionários")
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan, self.studio)

    def test_superadmin_view_blocked_too(self):
        self.client.force_login(self.superadmin)
        response = self.client.post(
            f"/plataforma/assinantes/{self.tenant.id}/plano/", {"plan": self.profissional.pk}
        )
        self.assertEqual(response.status_code, 302)
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertIsNone(subscription.plan)


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
            {"name": "Plano Teste", "price": "99,90", "description": "", "order": "0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Plan.objects.filter(name="Plano Teste").exists())

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


VALID_CPF = "12345678909"
VALID_CNPJ = "11223333000104"


class CpfCnpjValidationTest(TestCase):
    def test_valid_cpf_passes(self):
        validate_cpf_cnpj(VALID_CPF)  # não levanta

    def test_valid_cnpj_passes(self):
        validate_cpf_cnpj(VALID_CNPJ)  # não levanta

    def test_formatted_cpf_passes(self):
        validate_cpf_cnpj("123.456.789-09")

    def test_wrong_check_digit_rejected(self):
        with self.assertRaises(ValidationError):
            validate_cpf_cnpj("12345678900")

    def test_all_same_digit_rejected(self):
        with self.assertRaises(ValidationError):
            validate_cpf_cnpj("11111111111")

    def test_wrong_length_rejected(self):
        with self.assertRaises(ValidationError):
            validate_cpf_cnpj("123")


class TrialTest(TestCase):
    def test_register_tenant_sets_5_day_trial(self):
        tenant, _ = register_tenant(name="Salão Trial", email="dono@trial.com", password="Senha@123")
        subscription = Subscription.objects.get(tenant=tenant)
        self.assertIsNotNone(subscription.trial_ends_at)
        delta = subscription.trial_ends_at - timezone.now()
        self.assertAlmostEqual(delta.days, 5, delta=1)

    def test_trial_days_constant_only_affects_new_subscriptions_going_forward(self):
        """Regra do usuário em 2026-08-06 (e reforçada em 2026-08-08, ao
        reduzir de 7 para 5): mudar TRIAL_DAYS vale só pra cadastro novo —
        quem já tinha assinatura continua contando os dias que já tinha
        (`trial_ends_at` é gravado uma vez no cadastro, nunca recalculado
        depois a partir da constante)."""
        from unittest.mock import patch

        with patch("apps.billing.services.TRIAL_DAYS", 14):
            old_tenant, _ = register_tenant(
                name="Salão Antigo", email="antigo@trial.com", password="Senha@123"
            )
        old_subscription = Subscription.objects.get(tenant=old_tenant)
        self.assertAlmostEqual(
            (old_subscription.trial_ends_at - timezone.now()).days, 14, delta=1
        )

        new_tenant, _ = register_tenant(
            name="Salão Novo", email="novo@trial.com", password="Senha@123"
        )
        new_subscription = Subscription.objects.get(tenant=new_tenant)
        self.assertAlmostEqual(
            (new_subscription.trial_ends_at - timezone.now()).days, 5, delta=1
        )

        # a assinatura antiga não foi retroativamente encurtada
        old_subscription.refresh_from_db()
        self.assertAlmostEqual(
            (old_subscription.trial_ends_at - timezone.now()).days, 14, delta=1
        )


class AsaasClientTest(TestCase):
    def test_raises_when_api_key_not_configured(self):
        with override_settings(ASAAS_API_KEY=""):
            with self.assertRaises(asaas_client.AsaasNotConfigured):
                asaas_client.create_customer(name="Salão X", cpf_cnpj=VALID_CPF)

    @override_settings(ASAAS_API_KEY="test-key", ASAAS_ENV="sandbox")
    @patch("apps.billing.asaas_client.requests.request")
    def test_create_customer_sends_expected_payload(self, mock_request):
        mock_request.return_value.status_code = 200
        mock_request.return_value.content = json.dumps({"id": "cus_123"}).encode()
        mock_request.return_value.json.return_value = {"id": "cus_123"}

        result = asaas_client.create_customer(
            name="Salão X", cpf_cnpj="123.456.789-09", external_reference="tenant-1"
        )

        self.assertEqual(result["id"], "cus_123")
        call = mock_request.call_args
        self.assertEqual(call.args[0], "POST")
        self.assertIn("api-sandbox.asaas.com", call.args[1])
        self.assertEqual(call.kwargs["headers"]["access_token"], "test-key")
        self.assertEqual(call.kwargs["json"]["cpfCnpj"], "12345678909")  # só dígitos

    @override_settings(ASAAS_API_KEY="test-key")
    @patch("apps.billing.asaas_client.requests.request")
    def test_error_response_raises_asaas_error(self, mock_request):
        mock_request.return_value.status_code = 400
        mock_request.return_value.text = "erro"
        mock_request.return_value.json.return_value = {
            "errors": [{"description": "cpfCnpj inválido"}]
        }
        with self.assertRaises(asaas_client.AsaasError):
            asaas_client.create_customer(name="Salão X", cpf_cnpj="000")


class CheckoutServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-checkout")
        cls.plan = Plan.objects.get(name="Profissional")  # seedado por billing/migrations/0005

    def test_blocks_checkout_without_document(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        billing_ops.select_plan(subscription, self.plan)
        with self.assertRaises(ValidationError):
            billing_ops.get_or_create_checkout_url(subscription)

    def test_blocks_checkout_without_plan(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.tenant.document = VALID_CPF
        self.tenant.save(update_fields=["document"])
        with self.assertRaises(ValidationError):
            billing_ops.get_or_create_checkout_url(subscription)

    @patch("apps.billing.asaas_client.get_first_invoice_url")
    @patch("apps.billing.asaas_client.create_subscription")
    @patch("apps.billing.asaas_client.create_customer")
    def test_happy_path_creates_customer_and_subscription(
        self, mock_create_customer, mock_create_subscription, mock_invoice_url
    ):
        mock_create_customer.return_value = {"id": "cus_123"}
        mock_create_subscription.return_value = {"id": "sub_456"}
        mock_invoice_url.return_value = "https://sandbox.asaas.com/i/abc123"

        self.tenant.document = VALID_CPF
        self.tenant.save(update_fields=["document"])
        subscription = Subscription.objects.get(tenant=self.tenant)
        billing_ops.select_plan(subscription, self.plan)

        url = billing_ops.get_or_create_checkout_url(subscription, admin_email=self.admin.email)

        self.assertEqual(url, "https://sandbox.asaas.com/i/abc123")
        subscription.refresh_from_db()
        self.assertEqual(subscription.asaas_customer_id, "cus_123")
        self.assertEqual(subscription.asaas_subscription_id, "sub_456")
        self.assertEqual(subscription.status, SubscriptionStatus.PENDING)
        mock_create_customer.assert_called_once()
        mock_create_subscription.assert_called_once()

    @patch("apps.billing.asaas_client.get_first_invoice_url")
    @patch("apps.billing.asaas_client.create_subscription")
    @patch("apps.billing.asaas_client.create_customer")
    def test_reuses_existing_pending_checkout_without_recreating(
        self, mock_create_customer, mock_create_subscription, mock_invoice_url
    ):
        self.tenant.document = VALID_CPF
        self.tenant.save(update_fields=["document"])
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.plan
        subscription.status = SubscriptionStatus.PENDING
        subscription.asaas_customer_id = "cus_existing"
        subscription.asaas_subscription_id = "sub_existing"
        subscription.save()
        mock_invoice_url.return_value = "https://sandbox.asaas.com/i/existing"

        url = billing_ops.get_or_create_checkout_url(subscription)

        self.assertEqual(url, "https://sandbox.asaas.com/i/existing")
        mock_create_customer.assert_not_called()
        mock_create_subscription.assert_not_called()

    def test_blocks_checkout_when_already_active(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.plan
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save()
        self.tenant.document = VALID_CPF
        self.tenant.save(update_fields=["document"])
        with self.assertRaises(ValidationError):
            billing_ops.get_or_create_checkout_url(subscription)


class CancelSubscriptionServiceTest(TestCase):
    """Cancelamento self-service (decisão do usuário em 2026-08-08): só
    impede a próxima cobrança, não mexe em status/current_period_end."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-cancelar")
        cls.plan = Plan.objects.get(name="Profissional")

    def _active_recurring_subscription(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.plan
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.asaas_customer_id = "cus_1"
        subscription.asaas_subscription_id = "sub_1"
        subscription.current_period_start = timezone.localdate()
        subscription.current_period_end = timezone.localdate() + datetime.timedelta(days=20)
        subscription.save()
        return subscription

    def test_blocks_when_not_recurring(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.plan
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save()
        with self.assertRaises(ValidationError):
            billing_ops.cancel_subscription(subscription)

    def test_blocks_when_already_canceled(self):
        subscription = self._active_recurring_subscription()
        subscription.canceled_at = timezone.now()
        subscription.save()
        with self.assertRaises(ValidationError):
            billing_ops.cancel_subscription(subscription)

    @patch("apps.billing.asaas_client.cancel_subscription")
    def test_happy_path_calls_asaas_and_marks_canceled_at(self, mock_cancel):
        subscription = self._active_recurring_subscription()
        period_end_before = subscription.current_period_end
        status_before = subscription.status

        billing_ops.cancel_subscription(subscription)

        mock_cancel.assert_called_once_with("sub_1")
        subscription.refresh_from_db()
        self.assertIsNotNone(subscription.canceled_at)
        # não mexe no acesso já pago nem no status — só marca a intenção.
        self.assertEqual(subscription.current_period_end, period_end_before)
        self.assertEqual(subscription.status, status_before)

    @patch("apps.billing.asaas_client.cancel_subscription")
    def test_asaas_error_does_not_mark_canceled(self, mock_cancel):
        mock_cancel.side_effect = asaas_client.AsaasError("falha de rede")
        subscription = self._active_recurring_subscription()
        with self.assertRaises(asaas_client.AsaasError):
            billing_ops.cancel_subscription(subscription)
        subscription.refresh_from_db()
        self.assertIsNone(subscription.canceled_at)


class TransactionHistoryServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-historico")
        cls.plan = Plan.objects.get(name="Profissional")

    def test_none_when_not_recurring(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertIsNone(billing_ops.get_transaction_history(subscription))

    @patch("apps.billing.asaas_client.get_subscription_payments")
    def test_normalizes_payment_payload(self, mock_payments):
        mock_payments.return_value = [
            {
                "dueDate": "2026-08-07",
                "value": 129.9,
                "status": "CONFIRMED",
                "invoiceUrl": "https://sandbox.asaas.com/i/xyz",
            },
        ]
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.asaas_subscription_id = "sub_hist"
        subscription.save()

        history = billing_ops.get_transaction_history(subscription)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["due_date"], datetime.date(2026, 8, 7))
        self.assertEqual(history[0]["value"], Decimal("129.90"))
        self.assertEqual(history[0]["status"], "Confirmada")
        self.assertEqual(history[0]["invoice_url"], "https://sandbox.asaas.com/i/xyz")

    @patch("apps.billing.asaas_client.get_subscription_payments")
    def test_none_when_asaas_call_fails(self, mock_payments):
        mock_payments.side_effect = asaas_client.AsaasError("indisponível")
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.asaas_subscription_id = "sub_hist"
        subscription.save()
        self.assertIsNone(billing_ops.get_transaction_history(subscription))


class WebhookServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-webhook")
        cls.plan = Plan.objects.get(name="Profissional")  # seedado por billing/migrations/0005

    def _pending_subscription(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.plan
        subscription.status = SubscriptionStatus.PENDING
        subscription.asaas_subscription_id = "sub_789"
        subscription.save()
        return subscription

    def test_payment_confirmed_activates_subscription(self):
        self._pending_subscription()
        billing_ops.handle_asaas_webhook(
            event_type="PAYMENT_CONFIRMED",
            payment_data={"id": "pay_1", "subscription": "sub_789"},
        )
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(subscription.status, SubscriptionStatus.ACTIVE)
        self.assertIsNotNone(subscription.current_period_end)
        self.assertIsNotNone(subscription.first_active_at)

    def test_first_active_at_not_overwritten_on_renewal(self):
        """"Data de adesão" (2026-08-08) é a 1ª ativação — renovações
        seguintes reiniciam current_period_start/end, mas não isso."""
        self._pending_subscription()
        billing_ops.handle_asaas_webhook(
            event_type="PAYMENT_CONFIRMED",
            payment_data={"id": "pay_1", "subscription": "sub_789"},
        )
        subscription = Subscription.objects.get(tenant=self.tenant)
        first_active_at = subscription.first_active_at

        billing_ops.handle_asaas_webhook(
            event_type="PAYMENT_CONFIRMED",
            payment_data={"id": "pay_1_renewal", "subscription": "sub_789"},
        )
        subscription.refresh_from_db()
        self.assertEqual(subscription.first_active_at, first_active_at)

    def test_payment_overdue_marks_subscription_overdue(self):
        subscription = self._pending_subscription()
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save()
        billing_ops.handle_asaas_webhook(
            event_type="PAYMENT_OVERDUE",
            payment_data={"id": "pay_2", "subscription": "sub_789"},
        )
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, SubscriptionStatus.OVERDUE)

    def test_subscription_deleted_cancels_subscription(self):
        subscription = self._pending_subscription()
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save()
        billing_ops.handle_asaas_webhook(
            event_type="SUBSCRIPTION_DELETED",
            payment_data={"id": "pay_3", "subscription": "sub_789"},
        )
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, SubscriptionStatus.CANCELED)

    def test_unknown_asaas_subscription_id_is_ignored(self):
        billing_ops.handle_asaas_webhook(
            event_type="PAYMENT_CONFIRMED",
            payment_data={"id": "pay_4", "subscription": "sub_does_not_exist"},
        )  # não levanta, só ignora

    def test_duplicate_event_is_not_reprocessed(self):
        self._pending_subscription()
        payment_data = {"id": "pay_5", "subscription": "sub_789"}
        billing_ops.handle_asaas_webhook(event_type="PAYMENT_CONFIRMED", payment_data=payment_data)

        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.status = SubscriptionStatus.OVERDUE  # simula estado mudado manualmente
        subscription.save()

        # Reenvio do MESMO evento (Asaas reenvia se não recebe 200 a tempo) —
        # não deve reativar por cima do estado atual.
        billing_ops.handle_asaas_webhook(event_type="PAYMENT_CONFIRMED", payment_data=payment_data)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, SubscriptionStatus.OVERDUE)
        self.assertEqual(
            AsaasWebhookEvent.objects.filter(payment_id="pay_5", event_type="PAYMENT_CONFIRMED").count(),
            1,
        )


class WebhookViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-webhookview")
        cls.plan = Plan.objects.get(name="Profissional")  # seedado por billing/migrations/0005
        subscription = Subscription.objects.get(tenant=cls.tenant)
        subscription.plan = cls.plan
        subscription.status = SubscriptionStatus.PENDING
        subscription.asaas_subscription_id = "sub_view_1"
        subscription.save()

    @override_settings(ASAAS_WEBHOOK_TOKEN="segredo-123")
    def test_missing_token_rejected(self):
        response = self.client.post(
            "/webhooks/asaas/",
            data=json.dumps({"event": "PAYMENT_CONFIRMED", "payment": {"id": "p1", "subscription": "sub_view_1"}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(ASAAS_WEBHOOK_TOKEN="segredo-123")
    def test_wrong_token_rejected(self):
        response = self.client.post(
            "/webhooks/asaas/",
            data=json.dumps({"event": "PAYMENT_CONFIRMED", "payment": {"id": "p1", "subscription": "sub_view_1"}}),
            content_type="application/json",
            HTTP_ASAAS_ACCESS_TOKEN="token-errado",
        )
        self.assertEqual(response.status_code, 403)
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(subscription.status, SubscriptionStatus.PENDING)

    @override_settings(ASAAS_WEBHOOK_TOKEN="segredo-123")
    def test_valid_token_activates_subscription(self):
        response = self.client.post(
            "/webhooks/asaas/",
            data=json.dumps({"event": "PAYMENT_CONFIRMED", "payment": {"id": "p2", "subscription": "sub_view_1"}}),
            content_type="application/json",
            HTTP_ASAAS_ACCESS_TOKEN="segredo-123",
        )
        self.assertEqual(response.status_code, 200)
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(subscription.status, SubscriptionStatus.ACTIVE)


class MyPlanPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-meuplano")
        cls.plan = Plan.objects.get(name="Individual")  # seedado por billing/migrations/0005

    def test_login_required(self):
        response = self.client.get("/painel/plano/")
        self.assertEqual(response.status_code, 302)

    def test_shows_plan_cards(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertContains(response, "Individual")

    def test_plan_cards_show_employee_limits(self):
        """RF: cartões refletem o limite de funcionário de cada plano
        (decisão do usuário em 2026-08-04)."""
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertContains(response, "Studio")
        self.assertContains(response, "Até 3 funcionários")
        self.assertContains(response, "Até 6 funcionários")
        self.assertContains(response, "sem conta de funcionário extra")

    def test_plan_max_employees_seeded_correctly(self):
        # Individual = 1 (o próprio dono, decisão de 2026-08-07 — antes era
        # 0 porque o dono não contava; agora ele ocupa a vaga).
        self.assertEqual(Plan.objects.get(name="Individual").max_employees, 1)
        self.assertEqual(Plan.objects.get(name="Profissional").max_employees, 3)
        self.assertEqual(Plan.objects.get(name="Studio").max_employees, 6)

    def test_select_plan_sets_subscription_plan(self):
        self.client.force_login(self.admin)
        self.client.post(f"/painel/plano/assinar/{self.plan.pk}/")
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(subscription.plan, self.plan)

    def test_checkout_without_document_shows_document_form(self):
        self.client.force_login(self.admin)
        self.client.post(f"/painel/plano/assinar/{self.plan.pk}/")
        response = self.client.get("/painel/plano/checkout/")
        self.assertContains(response, "CPF")

    def test_submit_invalid_document_shows_error(self):
        self.client.force_login(self.admin)
        self.client.post(f"/painel/plano/assinar/{self.plan.pk}/")
        self.client.post("/painel/plano/checkout/documento/", {"document": "123"})
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.document, "")

    def test_submit_valid_document_saves_it(self):
        self.client.force_login(self.admin)
        self.client.post(f"/painel/plano/assinar/{self.plan.pk}/")
        self.client.post("/painel/plano/checkout/documento/", {"document": VALID_CPF})
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.document, VALID_CPF)

    def test_checkout_without_credentials_shows_friendly_error(self):
        """Sem ASAAS_API_KEY no .env (ambiente sem credencial ainda) — a
        tela não deve quebrar, só avisar que o pagamento não está ativo."""
        self.client.force_login(self.admin)
        self.tenant.document = VALID_CPF
        self.tenant.save(update_fields=["document"])
        self.client.post(f"/painel/plano/assinar/{self.plan.pk}/")
        with override_settings(ASAAS_API_KEY=""):
            response = self.client.get("/painel/plano/checkout/")
        self.assertContains(response, "credenciais do Asaas pendentes")


class CancelSubscriptionViewTest(TestCase):
    """Botão "Cancelar assinatura" em Meu Plano (2026-08-08)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-cancelarview")
        cls.plan = Plan.objects.get(name="Profissional")

    def _active_recurring_subscription(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.plan
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.asaas_customer_id = "cus_1"
        subscription.asaas_subscription_id = "sub_1"
        subscription.current_period_start = timezone.localdate()
        subscription.current_period_end = timezone.localdate() + datetime.timedelta(days=20)
        subscription.save()
        return subscription

    def test_cancel_button_hidden_when_not_recurring(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.plan
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertNotContains(response, "Cancelar assinatura")

    def test_cancel_button_shown_when_active_and_recurring(self):
        self._active_recurring_subscription()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertContains(response, "Cancelar assinatura")

    def test_confirm_modal_mentions_period_end_date(self):
        subscription = self._active_recurring_subscription()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/cancelar/confirmar/")
        self.assertContains(response, subscription.current_period_end.strftime("%d/%m/%Y"))

    @patch("apps.billing.asaas_client.cancel_subscription")
    def test_post_cancels_and_redirects(self, mock_cancel):
        subscription = self._active_recurring_subscription()
        self.client.force_login(self.admin)
        response = self.client.post("/painel/plano/cancelar/")
        self.assertEqual(response.headers.get("HX-Redirect"), "/painel/plano/")
        mock_cancel.assert_called_once_with("sub_1")
        subscription.refresh_from_db()
        self.assertIsNotNone(subscription.canceled_at)
        # acesso não é cortado na hora — status/período continuam intactos.
        self.assertEqual(subscription.status, SubscriptionStatus.ACTIVE)

    @patch("apps.billing.asaas_client.cancel_subscription")
    def test_after_cancel_access_continues_until_period_end(self, mock_cancel):
        subscription = self._active_recurring_subscription()
        self.client.force_login(self.admin)
        self.client.post("/painel/plano/cancelar/")
        response = self.client.get("/painel/clientes/")
        self.assertEqual(response.status_code, 200)

    def test_cancel_button_hidden_once_already_canceled(self):
        subscription = self._active_recurring_subscription()
        subscription.canceled_at = timezone.now()
        subscription.save()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertNotContains(response, "Cancelar assinatura")
        self.assertContains(response, "sem nova cobrança agendada")

    @patch("apps.billing.asaas_client.get_subscription_payments")
    def test_shows_transaction_history_table(self, mock_payments):
        mock_payments.return_value = [
            {"dueDate": "2026-08-07", "value": 129.9, "status": "CONFIRMED", "invoiceUrl": "https://x"},
        ]
        self._active_recurring_subscription()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertContains(response, "Histórico de transações")
        self.assertContains(response, "Confirmada")
        self.assertContains(response, "129,90")


class SubscriptionBlocksPanelAccessTest(TestCase):
    """RF30 — decisão do usuário em 2026-07-31: `canceled` bloqueia na hora;
    `overdue` só depois do `grace_period_days` contado do fim do último
    período pago; `trialing` bloqueia assim que `trial_ends_at` passa, sem
    tolerância extra; `pending`/`active` nunca bloqueiam."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-bloqueio")

    def _set_subscription(self, **fields):
        subscription = Subscription.objects.get(tenant=self.tenant)
        for field, value in fields.items():
            setattr(subscription, field, value)
        subscription.save()
        return subscription

    def test_trialing_within_period_does_not_block(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.TRIALING,
            trial_ends_at=timezone.now() + datetime.timedelta(days=3),
        )
        self.assertFalse(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_trialing_after_trial_ends_at_blocks(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.TRIALING,
            trial_ends_at=timezone.now() - datetime.timedelta(minutes=1),
        )
        self.assertTrue(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_trialing_without_trial_ends_at_does_not_block(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.TRIALING, trial_ends_at=None,
        )
        self.assertFalse(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_active_never_blocks(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.ACTIVE,
            current_period_end=timezone.localdate() - datetime.timedelta(days=100),
        )
        self.assertFalse(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_pending_never_blocks(self):
        subscription = self._set_subscription(status=SubscriptionStatus.PENDING)
        self.assertFalse(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_overdue_within_grace_period_does_not_block(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.OVERDUE,
            current_period_end=timezone.localdate() - datetime.timedelta(days=2),
            grace_period_days=5,
        )
        self.assertFalse(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_overdue_past_grace_period_blocks(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.OVERDUE,
            current_period_end=timezone.localdate() - datetime.timedelta(days=10),
            grace_period_days=5,
        )
        self.assertTrue(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_overdue_without_known_period_blocks_immediately(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.OVERDUE, current_period_end=None,
        )
        self.assertTrue(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_canceled_blocks_immediately(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.CANCELED,
            current_period_end=timezone.localdate(),
        )
        self.assertTrue(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_active_with_self_cancellation_before_period_end_does_not_block(self):
        """Decisão do usuário em 2026-08-08: cancelar em Meu Plano só impede
        a PRÓXIMA cobrança — acesso continua até o período já pago acabar."""
        subscription = self._set_subscription(
            status=SubscriptionStatus.ACTIVE,
            current_period_end=timezone.localdate() + datetime.timedelta(days=5),
            canceled_at=timezone.now(),
        )
        self.assertFalse(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_active_with_self_cancellation_after_period_end_blocks(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.ACTIVE,
            current_period_end=timezone.localdate() - datetime.timedelta(days=1),
            canceled_at=timezone.now(),
        )
        self.assertTrue(billing_ops.subscription_blocks_panel_access(subscription.tenant))

    def test_active_with_self_cancellation_and_unknown_period_blocks(self):
        subscription = self._set_subscription(
            status=SubscriptionStatus.ACTIVE, current_period_end=None, canceled_at=timezone.now(),
        )
        self.assertTrue(billing_ops.subscription_blocks_panel_access(subscription.tenant))


class SidebarPlanExpiredTest(TestCase):
    """`apps.billing.context_processors.sidebar_plan` — extensão de
    2026-07-31: mostra "expirado" em vez de "0 dias restantes" depois que o
    trial passou."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-sidebartrial")

    def _set_trial_ends_at(self, value):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.trial_ends_at = value
        subscription.save(update_fields=["trial_ends_at"])

    def test_within_period_shows_days_left(self):
        self._set_trial_ends_at(timezone.now() + datetime.timedelta(days=3))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertContains(response, "3 dias restantes")
        self.assertNotContains(response, "expirado")

    def test_after_trial_ends_at_shows_expired(self):
        self._set_trial_ends_at(timezone.now() - datetime.timedelta(days=1))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertContains(response, "Gratuito")
        self.assertContains(response, "expirado")
        self.assertNotContains(response, "dias restante")


class SidebarPlanUrgencyTest(TestCase):
    """`apps.billing.context_processors.sidebar_plan` — cor progressiva do
    contador de dias (decisão do usuário em 2026-08-07), só nos dois casos
    com risco real de bloqueio (trial e carência do atraso); plano ativo
    contando pra próxima cobrança normal nunca fica "urgente"."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-urgencia")
        cls.plan = Plan.objects.get(name="Profissional")

    def _set_trial_ends_at(self, value):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.trial_ends_at = value
        subscription.save(update_fields=["trial_ends_at"])

    def _set_overdue(self, current_period_end):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.plan
        subscription.status = SubscriptionStatus.OVERDUE
        subscription.current_period_end = current_period_end
        subscription.grace_period_days = 5
        subscription.save()

    def test_trial_far_from_expiring_has_no_urgency(self):
        self._set_trial_ends_at(timezone.now() + datetime.timedelta(days=6))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertIsNone(response.context["sidebar_plan_urgency"])

    def test_trial_3_days_left_is_warning(self):
        self._set_trial_ends_at(timezone.now() + datetime.timedelta(days=3))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertEqual(response.context["sidebar_plan_urgency"], "warning")

    def test_trial_1_day_left_is_urgent(self):
        self._set_trial_ends_at(timezone.now() + datetime.timedelta(days=1))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertEqual(response.context["sidebar_plan_urgency"], "urgent")

    def test_trial_expired_is_critical(self):
        self._set_trial_ends_at(timezone.now() - datetime.timedelta(days=1))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertEqual(response.context["sidebar_plan_urgency"], "critical")

    def test_overdue_shows_grace_countdown_not_gratuito(self):
        """Correção de 2026-08-07: antes caía no mesmo ramo do trial e
        mostrava "Gratuito" com base em `trial_ends_at` (irrelevante pra
        quem já foi pagante)."""
        self._set_overdue(current_period_end=timezone.localdate() - datetime.timedelta(days=2))
        # grace_period_days=5, período acabou há 2 dias -> 3 dias de carência.
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertEqual(response.context["sidebar_plan_days_left"], 3)
        self.assertEqual(response.context["sidebar_plan_urgency"], "warning")
        self.assertIn("atrasado", response.context["sidebar_plan_label"])
        self.assertNotEqual(response.context["sidebar_plan_label"], "Gratuito")

    def test_overdue_last_day_of_grace_is_critical(self):
        self._set_overdue(current_period_end=timezone.localdate() - datetime.timedelta(days=5))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertEqual(response.context["sidebar_plan_days_left"], 0)
        self.assertEqual(response.context["sidebar_plan_urgency"], "critical")

    def test_active_plan_never_gets_urgency_even_near_renewal(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.plan = self.plan
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.current_period_end = timezone.localdate() + datetime.timedelta(days=1)
        subscription.save()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/")
        self.assertEqual(response.context["sidebar_plan_days_left"], 1)
        self.assertIsNone(response.context["sidebar_plan_urgency"])


class PanelAccessBlockedPanelTest(TestCase):
    """Integração do RF30 com os decorators de painel
    (`apps.accounts.decorators`) — tenant_admin é redirecionado pra
    /painel/plano/ (única exceção liberada), funcionário recebe 403."""

    @classmethod
    def setUpTestData(cls):
        from apps.employees.services import create_employee

        cls.tenant, cls.admin = make_tenant_with_admin("salao-bloqueado")
        cls.employee = create_employee(
            tenant=cls.tenant,
            full_name="Ana Silva",
            email="ana@salao-bloqueado.com",
            password="Senha@123",
            default_commission_type="percentage",
            default_commission_value=Decimal("40"),
        )

    def _block_subscription(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.status = SubscriptionStatus.CANCELED
        subscription.save(update_fields=["status", "updated_at"])

    def test_blocked_admin_redirected_to_my_plan(self):
        self._block_subscription()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/")
        self.assertRedirects(response, "/painel/plano/")

    def test_blocked_admin_can_still_reach_my_plan(self):
        self._block_subscription()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assinatura cancelada")

    def test_blocked_admin_can_still_checkout(self):
        self._block_subscription()
        # "Individual" (max_employees=0) não serve aqui — setUpTestData já
        # cria 1 funcionário ativo (Ana Silva), e a trava de downgrade
        # (2026-08-07) bloquearia por motivo alheio ao que este teste cobre.
        plan = Plan.objects.get(name="Profissional")
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/plano/assinar/{plan.pk}/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/painel/plano/checkout/")

    def test_blocked_employee_forbidden(self):
        self._block_subscription()
        self.client.force_login(self.employee.user)
        response = self.client.get("/painel/minha-agenda/")
        self.assertEqual(response.status_code, 403)

    def test_not_blocked_admin_uses_panel_normally(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/")
        self.assertEqual(response.status_code, 200)

    def test_blocked_tenant_public_page_still_works(self):
        """RF30 só afeta o painel administrativo — a página pública de
        agendamento do salão continua no ar mesmo com a assinatura da
        plataforma bloqueada."""
        self._block_subscription()
        response = self.client.get(f"/{self.tenant.slug}/")
        self.assertEqual(response.status_code, 200)


class TrialExpiredPanelBlockTest(TestCase):
    """RF30 — extensão de 2026-07-31: mesmo bloqueio de `PanelAccessBlockedPanelTest`,
    mas pro caso "trial gratuito acabou" (status continua `trialing`, só
    `trial_ends_at` que já passou — nenhum job muda o status)."""

    @classmethod
    def setUpTestData(cls):
        from apps.employees.services import create_employee

        cls.tenant, cls.admin = make_tenant_with_admin("salao-trialvencido")
        cls.employee = create_employee(
            tenant=cls.tenant,
            full_name="Ana Silva",
            email="ana@salao-trialvencido.com",
            password="Senha@123",
            default_commission_type="percentage",
            default_commission_value=Decimal("40"),
        )

    def _expire_trial(self):
        subscription = Subscription.objects.get(tenant=self.tenant)
        subscription.trial_ends_at = timezone.now() - datetime.timedelta(days=1)
        subscription.save(update_fields=["trial_ends_at"])

    def test_expired_trial_admin_redirected_to_my_plan(self):
        self._expire_trial()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/")
        self.assertRedirects(response, "/painel/plano/")

    def test_expired_trial_admin_sees_expired_banner(self):
        self._expire_trial()
        self.client.force_login(self.admin)
        response = self.client.get("/painel/plano/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Seu período de teste grátis acabou")

    def test_expired_trial_admin_can_still_select_plan_and_checkout(self):
        self._expire_trial()
        # Mesmo motivo do análogo em PanelAccessBlockedPanelTest: setUpTestData
        # já cria 1 funcionário ativo, "Individual" (max_employees=0) bateria
        # na trava de downgrade por um motivo que não é o que este teste cobre.
        plan = Plan.objects.get(name="Profissional")
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/plano/assinar/{plan.pk}/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/painel/plano/checkout/")

    def test_expired_trial_employee_forbidden(self):
        self._expire_trial()
        self.client.force_login(self.employee.user)
        response = self.client.get("/painel/minha-agenda/")
        self.assertEqual(response.status_code, 403)

    def test_expired_trial_public_page_still_works(self):
        self._expire_trial()
        response = self.client.get(f"/{self.tenant.slug}/")
        self.assertEqual(response.status_code, 200)

    def test_trial_within_period_admin_uses_panel_normally(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/clientes/")
        self.assertEqual(response.status_code, 200)
