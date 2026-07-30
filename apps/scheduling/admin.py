from django.contrib import admin

from .models import Appointment


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("date", "start_time", "employee", "client", "service", "status")
    list_filter = ("status", "tenant")
    search_fields = ("client__name", "client__phone", "employee__full_name")
