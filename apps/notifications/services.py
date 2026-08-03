from django.db import transaction
from django.utils import timezone

from .models import Announcement, AnnouncementRead, TenantNotification


def create_announcement(*, title, message, created_by):
    return Announcement.objects.create(title=title, message=message, created_by=created_by)


def update_announcement(announcement, *, title, message):
    announcement.title = title
    announcement.message = message
    announcement.save(update_fields=["title", "message"])
    return announcement


def set_announcement_active(announcement, is_active):
    announcement.is_active = is_active
    announcement.save(update_fields=["is_active"])
    return announcement


def unread_announcements_for_user(user):
    return Announcement.objects.filter(is_active=True).exclude(reads__user=user).order_by(
        "-created_at"
    )


def unread_count_for_user(user):
    return unread_announcements_for_user(user).count()


def mark_read(announcement, user):
    AnnouncementRead.objects.get_or_create(announcement=announcement, user=user)


@transaction.atomic
def mark_all_read(user):
    AnnouncementRead.objects.bulk_create(
        [AnnouncementRead(announcement=a, user=user) for a in unread_announcements_for_user(user)],
        ignore_conflicts=True,
    )


# ---------------------------------------------------------------------------
# Notificações operacionais do tenant (diferente de Announcement — ver
# apps.notifications.models.TenantNotification)
# ---------------------------------------------------------------------------


def create_tenant_notification(tenant, *, kind, title, message, appointment=None):
    return TenantNotification.objects.create(
        tenant=tenant, kind=kind, title=title, message=message, appointment=appointment,
    )


def unread_tenant_notifications(tenant):
    return TenantNotification.objects.for_tenant(tenant).filter(is_read=False)


def unread_tenant_notification_count(tenant):
    return unread_tenant_notifications(tenant).count()


def new_tenant_notifications_since(tenant, last_seen_id):
    """Usado pelo polling de toast (`agenda_toast_poll`) — não depende de
    `is_read` (o admin pode ver o toast sem "abrir" a notificação, e ela
    continua não lida no sininho até ele marcar)."""
    return TenantNotification.objects.for_tenant(tenant).filter(pk__gt=last_seen_id).order_by("pk")


def mark_tenant_notification_read(notification):
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at"])
    return notification


@transaction.atomic
def mark_all_tenant_notifications_read(tenant):
    unread_tenant_notifications(tenant).update(is_read=True, read_at=timezone.now())
