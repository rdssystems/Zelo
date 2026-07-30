from django.contrib import admin

from .models import CashTransaction, Commission


@admin.register(CashTransaction)
class CashTransactionAdmin(admin.ModelAdmin):
    list_display = ("type", "category", "amount", "payment_method", "tenant", "created_at")
    list_filter = ("type", "category", "tenant")
    search_fields = ("description",)
    readonly_fields = ("created_at",)


@admin.register(Commission)
class CommissionAdmin(admin.ModelAdmin):
    list_display = ("employee", "appointment", "calculated_amount", "status", "tenant")
    list_filter = ("status", "tenant")
    readonly_fields = (
        "commission_type",
        "commission_value",
        "base_amount",
        "calculated_amount",
    )
