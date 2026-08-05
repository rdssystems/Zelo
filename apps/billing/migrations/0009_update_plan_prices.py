from django.db import migrations

# Decisão do usuário em 2026-08-05: reajuste de preço do Individual e do
# Profissional. Studio não muda.
PRICE_UPDATES = [
    {"name": "Individual", "old_price": "49.90", "new_price": "69.90"},
    {"name": "Profissional", "old_price": "99.90", "new_price": "129.90"},
]


def apply_price_updates(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for data in PRICE_UPDATES:
        Plan.objects.filter(name=data["name"]).update(price=data["new_price"])


def revert_price_updates(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for data in PRICE_UPDATES:
        Plan.objects.filter(name=data["name"]).update(price=data["old_price"])


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0008_plan_max_employees"),
    ]

    operations = [
        migrations.RunPython(apply_price_updates, revert_price_updates),
    ]
