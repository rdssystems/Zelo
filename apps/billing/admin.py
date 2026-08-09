from django.contrib import admin

from .models import Plan, PlatformSettings, Subscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "is_active", "order")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(PlatformSettings)
class PlatformSettingsAdmin(admin.ModelAdmin):
    list_display = ("support_whatsapp",)

    def has_add_permission(self, request):
        # Singleton — só edita a linha única que já existe (get_solo cria na
        # 1ª vez que alguma tela chamar), não deixa criar uma segunda.
        return not PlatformSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "tenant", "plan", "status", "current_period_start", "current_period_end", "updated_at",
    )
    list_filter = ("status", "plan")
    search_fields = ("tenant__name",)
