from django.contrib import admin

from .models import Announcement, AnnouncementRead


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "is_active", "created_by", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "message")


@admin.register(AnnouncementRead)
class AnnouncementReadAdmin(admin.ModelAdmin):
    list_display = ("announcement", "user", "read_at")
    search_fields = ("user__email", "announcement__title")
