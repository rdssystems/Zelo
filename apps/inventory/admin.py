from django.contrib import admin

from .models import (
    Category,
    PhysicalInventoryCount,
    PhysicalInventoryCountItem,
    Product,
    ProductBatch,
    StockMovement,
    StockMovementBatch,
    Supplier,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant")
    list_filter = ("tenant",)
    search_fields = ("name",)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_name", "phone", "tenant", "is_active")
    list_filter = ("is_active", "tenant")
    search_fields = ("name", "contact_name", "phone", "email")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "category", "supplier", "tenant", "current_stock", "min_stock_alert", "is_active")
    list_filter = ("is_active", "category", "tenant")
    search_fields = ("name", "sku")
    readonly_fields = ("current_stock",)


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("product", "type", "quantity", "reason", "tenant", "created_at")
    list_filter = ("type", "reason", "tenant")
    search_fields = ("product__name",)
    readonly_fields = ("total_value", "created_at")


@admin.register(ProductBatch)
class ProductBatchAdmin(admin.ModelAdmin):
    list_display = ("product", "batch_number", "expiry_date", "quantity_received", "quantity_remaining", "tenant")
    list_filter = ("tenant",)
    search_fields = ("product__name", "batch_number")
    readonly_fields = ("received_at",)


@admin.register(StockMovementBatch)
class StockMovementBatchAdmin(admin.ModelAdmin):
    list_display = ("movement", "batch", "quantity", "tenant")
    list_filter = ("tenant",)


class PhysicalInventoryCountItemInline(admin.TabularInline):
    model = PhysicalInventoryCountItem
    extra = 0
    readonly_fields = ("product", "expected_quantity")


@admin.register(PhysicalInventoryCount)
class PhysicalInventoryCountAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "status", "started_at", "completed_at", "created_by")
    list_filter = ("status", "tenant")
    inlines = [PhysicalInventoryCountItemInline]
