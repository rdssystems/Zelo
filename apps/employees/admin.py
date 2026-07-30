from django.contrib import admin

from .models import Employee, EmployeeService, ScheduleException, WorkingHours


class WorkingHoursInline(admin.TabularInline):
    model = WorkingHours
    extra = 0


class EmployeeServiceInline(admin.TabularInline):
    model = EmployeeService
    extra = 0


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("full_name", "tenant", "phone", "default_commission_type", "is_active")
    list_filter = ("is_active", "tenant")
    search_fields = ("full_name", "user__email")
    inlines = [WorkingHoursInline, EmployeeServiceInline]


@admin.register(ScheduleException)
class ScheduleExceptionAdmin(admin.ModelAdmin):
    list_display = ("employee", "date", "start_time", "end_time", "reason")
    list_filter = ("tenant",)
