from django.db import transaction

from .models import Announcement, AnnouncementRead


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
