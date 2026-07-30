from django.db import migrations


def create_missing_subscriptions(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Subscription = apps.get_model("billing", "Subscription")
    for tenant in Tenant.objects.filter(subscription__isnull=True):
        Subscription.objects.create(tenant=tenant, status="trialing")


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_missing_subscriptions, noop_reverse),
    ]
