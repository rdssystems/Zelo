import datetime

from django.db import migrations

# Espelha apps/tenants/services.py::_DEFAULT_BUSINESS_HOURS — tenants que já
# existiam antes desse model nascem com o mesmo horário padrão sugerido
# (Seg-Sex 9h-19h, Sáb 9h-13h, Dom fechado), ajustável depois em Configurações.
_DEFAULT_BUSINESS_HOURS = [
    (0, False, datetime.time(9, 0), datetime.time(19, 0)),
    (1, False, datetime.time(9, 0), datetime.time(19, 0)),
    (2, False, datetime.time(9, 0), datetime.time(19, 0)),
    (3, False, datetime.time(9, 0), datetime.time(19, 0)),
    (4, False, datetime.time(9, 0), datetime.time(19, 0)),
    (5, False, datetime.time(9, 0), datetime.time(13, 0)),
    (6, True, None, None),
]


def create_missing_business_hours(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    TenantBusinessHours = apps.get_model("tenants", "TenantBusinessHours")
    for tenant in Tenant.objects.filter(tenants_tenantbusinesshours_set__isnull=True):
        TenantBusinessHours.objects.bulk_create(
            TenantBusinessHours(
                tenant=tenant, weekday=weekday, is_closed=is_closed,
                start_time=start_time, end_time=end_time,
            )
            for weekday, is_closed, start_time, end_time in _DEFAULT_BUSINESS_HOURS
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0003_remove_tenant_business_hours_note_and_more"),
    ]

    operations = [
        migrations.RunPython(create_missing_business_hours, noop_reverse),
    ]
