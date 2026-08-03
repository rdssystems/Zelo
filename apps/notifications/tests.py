from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.employees.services import create_employee
from apps.tenants.models import Tenant

from . import services as notif_ops
from .models import Announcement, AnnouncementRead

User = get_user_model()


def make_tenant_with_admin(slug):
    tenant = Tenant.objects.create(name=f"Salão {slug}", slug=slug)
    admin = User.objects.create_user(
        email=f"admin@{slug}.com", password="x", role=User.Role.TENANT_ADMIN, tenant=tenant
    )
    return tenant, admin


def make_superadmin(email="root@zelo.local"):
    return User.objects.create_user(
        email=email, password="x", role=User.Role.SUPERADMIN, tenant=None,
        is_staff=True, is_superuser=True,
    )


class AnnouncementDomainTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")
        cls.superadmin = make_superadmin()

    def test_new_announcement_is_unread_for_every_tenant_admin(self):
        notif_ops.create_announcement(
            title="Nova função", message="Chegou o CRM!", created_by=self.superadmin
        )
        self.assertEqual(notif_ops.unread_count_for_user(self.admin_a), 1)
        self.assertEqual(notif_ops.unread_count_for_user(self.admin_b), 1)

    def test_mark_read_only_affects_that_user(self):
        announcement = notif_ops.create_announcement(
            title="Nova função", message="Chegou o CRM!", created_by=self.superadmin
        )
        notif_ops.mark_read(announcement, self.admin_a)
        self.assertEqual(notif_ops.unread_count_for_user(self.admin_a), 0)
        self.assertEqual(notif_ops.unread_count_for_user(self.admin_b), 1)

    def test_mark_read_is_idempotent(self):
        announcement = notif_ops.create_announcement(
            title="Nova função", message="Chegou o CRM!", created_by=self.superadmin
        )
        notif_ops.mark_read(announcement, self.admin_a)
        notif_ops.mark_read(announcement, self.admin_a)
        self.assertEqual(AnnouncementRead.objects.filter(user=self.admin_a).count(), 1)

    def test_mark_all_read(self):
        notif_ops.create_announcement(title="A", message="msg a", created_by=self.superadmin)
        notif_ops.create_announcement(title="B", message="msg b", created_by=self.superadmin)
        notif_ops.mark_all_read(self.admin_a)
        self.assertEqual(notif_ops.unread_count_for_user(self.admin_a), 0)
        self.assertEqual(notif_ops.unread_count_for_user(self.admin_b), 2)

    def test_inactive_announcement_not_counted(self):
        announcement = notif_ops.create_announcement(
            title="Antigo", message="msg", created_by=self.superadmin
        )
        notif_ops.set_announcement_active(announcement, False)
        self.assertEqual(notif_ops.unread_count_for_user(self.admin_a), 0)


class AnnouncementPanelAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = create_employee(
            tenant=cls.tenant, full_name="Ana Silva", email="func@salao-a.com", password="Senha@123",
            default_commission_type="percentage", default_commission_value=Decimal("40.00"),
        )
        cls.employee_user = cls.employee.user
        cls.superadmin = make_superadmin()

    def test_superadmin_crud_forbidden_to_tenant_admin(self):
        self.client.force_login(self.admin)
        response = self.client.get("/plataforma/avisos/")
        self.assertEqual(response.status_code, 403)

    def test_superadmin_can_create_announcement(self):
        self.client.force_login(self.superadmin)
        response = self.client.post(
            "/plataforma/avisos/novo/", {"title": "Nova função", "message": "Chegou o CRM!"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Announcement.objects.filter(title="Nova função").exists())

    def test_tenant_admin_sees_bell_with_unread_count(self):
        notif_ops.create_announcement(
            title="Nova função", message="Chegou o CRM!", created_by=self.superadmin
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/servicos/")
        self.assertContains(response, "Notificações")
        self.assertContains(response, ">1<")

    def test_employee_does_not_see_bell(self):
        notif_ops.create_announcement(
            title="Nova função", message="Chegou o CRM!", created_by=self.superadmin
        )
        self.client.force_login(self.employee_user)
        response = self.client.get("/painel/minha-agenda/")
        self.assertNotContains(response, "id=\"notification-bell\"")

    def test_notification_list_forbidden_to_employee(self):
        self.client.force_login(self.employee_user)
        response = self.client.get("/painel/avisos/")
        self.assertEqual(response.status_code, 403)

    def test_mark_read_updates_bell_via_panel(self):
        announcement = notif_ops.create_announcement(
            title="Nova função", message="Chegou o CRM!", created_by=self.superadmin
        )
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/avisos/{announcement.pk}/marcar-lida/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(notif_ops.unread_count_for_user(self.admin), 0)


class TenantNotificationDomainTest(TestCase):
    """Alerta operacional do tenant (diferente de Announcement) — hoje só
    gerado por cancelamento pelo cliente (ver apps.scheduling.services)."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-notif-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-notif-b")

    def test_create_and_unread_count(self):
        notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="T", message="M",
        )
        self.assertEqual(notif_ops.unread_tenant_notification_count(self.tenant_a), 1)

    def test_isolated_by_tenant(self):
        notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="T", message="M",
        )
        self.assertEqual(notif_ops.unread_tenant_notification_count(self.tenant_b), 0)

    def test_mark_read(self):
        notification = notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="T", message="M",
        )
        notif_ops.mark_tenant_notification_read(notification)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertIsNotNone(notification.read_at)
        self.assertEqual(notif_ops.unread_tenant_notification_count(self.tenant_a), 0)

    def test_mark_all_read(self):
        notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="A", message="M",
        )
        notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="B", message="M",
        )
        notif_ops.mark_all_tenant_notifications_read(self.tenant_a)
        self.assertEqual(notif_ops.unread_tenant_notification_count(self.tenant_a), 0)

    def test_new_since_watermark(self):
        first = notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="A", message="M",
        )
        second = notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="B", message="M",
        )
        since_zero = list(notif_ops.new_tenant_notifications_since(self.tenant_a, 0))
        self.assertEqual(since_zero, [first, second])
        since_first = list(notif_ops.new_tenant_notifications_since(self.tenant_a, first.pk))
        self.assertEqual(since_first, [second])


class TenantNotificationPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-notifpanel-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-notifpanel-b")

    def test_bell_count_merges_announcements_and_tenant_notifications(self):
        superadmin = make_superadmin()
        notif_ops.create_announcement(title="Novo", message="msg", created_by=superadmin)
        notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="T", message="M",
        )
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/servicos/")
        self.assertContains(response, ">2<")

    def test_notification_list_shows_agenda_section(self):
        notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client",
            title="Maria cancelou", message="Detalhe do cancelamento",
        )
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/avisos/")
        self.assertContains(response, "Maria cancelou")
        self.assertContains(response, "Agenda")

    def test_notification_list_does_not_leak_other_tenant(self):
        notif_ops.create_tenant_notification(
            self.tenant_b, kind="appointment_canceled_by_client",
            title="Alerta do outro salão", message="M",
        )
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/avisos/")
        self.assertNotContains(response, "Alerta do outro salão")

    def test_mark_tenant_notification_read_via_panel(self):
        notification = notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="T", message="M",
        )
        self.client.force_login(self.admin_a)
        response = self.client.post(f"/painel/avisos/agenda/{notification.pk}/marcar-lida/")
        self.assertEqual(response.status_code, 200)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

    def test_cannot_mark_other_tenant_notification_read(self):
        notification = notif_ops.create_tenant_notification(
            self.tenant_b, kind="appointment_canceled_by_client", title="T", message="M",
        )
        self.client.force_login(self.admin_a)
        response = self.client.post(f"/painel/avisos/agenda/{notification.pk}/marcar-lida/")
        self.assertEqual(response.status_code, 404)

    def test_mark_all_read_clears_both_kinds(self):
        superadmin = make_superadmin()
        notif_ops.create_announcement(title="Novo", message="msg", created_by=superadmin)
        notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client", title="T", message="M",
        )
        self.client.force_login(self.admin_a)
        response = self.client.post("/painel/avisos/marcar-todas-lidas/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(notif_ops.unread_count_for_user(self.admin_a), 0)
        self.assertEqual(notif_ops.unread_tenant_notification_count(self.tenant_a), 0)

    def test_toast_poll_requires_login(self):
        response = self.client.get("/painel/avisos/toast/")
        self.assertEqual(response.status_code, 302)

    def test_toast_poll_shows_new_notification_once(self):
        notif_ops.create_tenant_notification(
            self.tenant_a, kind="appointment_canceled_by_client",
            title="Cliente cancelou", message="Detalhe",
        )
        self.client.force_login(self.admin_a)
        first = self.client.get("/painel/avisos/toast/")
        self.assertContains(first, "Cliente cancelou")
        second = self.client.get("/painel/avisos/toast/")
        self.assertNotContains(second, "Cliente cancelou")

    def test_toast_poll_does_not_leak_other_tenant(self):
        notif_ops.create_tenant_notification(
            self.tenant_b, kind="appointment_canceled_by_client",
            title="Alerta do outro salão", message="M",
        )
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/avisos/toast/")
        self.assertNotContains(response, "Alerta do outro salão")
