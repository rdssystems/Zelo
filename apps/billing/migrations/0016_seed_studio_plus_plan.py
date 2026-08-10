from django.db import migrations

# Decisão do usuário em 2026-08-10: 4º plano, acima do Studio, pra quem
# precisa de mais de 6 funcionários — `max_employees=None` já é "sem limite"
# nativamente no model (ver Plan.max_employees), reservado exatamente pra
# esse caso desde a migration 0008. Nome e preço definidos pelo usuário.
PLAN = {
    "name": "Studio Plus",
    "description": "Pra redes e equipes grandes — funcionários ilimitados.",
    "price": "249.90",
    "order": 4,
    "max_employees": None,
}


def seed_plan(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.get_or_create(
        name=PLAN["name"],
        defaults={
            "description": PLAN["description"],
            "price": PLAN["price"],
            "order": PLAN["order"],
            "max_employees": PLAN["max_employees"],
            "is_active": True,
        },
    )


def remove_plan(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(name=PLAN["name"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0015_platformsettings"),
    ]

    operations = [
        migrations.RunPython(seed_plan, remove_plan),
    ]
