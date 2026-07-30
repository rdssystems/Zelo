from django.contrib import admin

from .models import Service


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "duration_minutes", "price", "is_active")
    list_filter = ("is_active", "tenant")
    search_fields = ("name",)
