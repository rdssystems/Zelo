from django.contrib import admin

from .models import Plan, Subscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "is_active", "order")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "tenant", "plan", "status", "current_period_start", "current_period_end", "updated_at",
    )
    list_filter = ("status", "plan")
    search_fields = ("tenant__name",)
