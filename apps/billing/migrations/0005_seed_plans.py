from django.db import migrations

# Conteúdo/preço decidido com o usuário em 2026-07-30 (ver 01-REQUISITOS.md
# §4.2) — o texto de benefícios (highlights) fica em
# apps/billing/views.py::PLAN_HIGHLIGHTS, não aqui; esta migration só cria
# o registro que a assinatura/checkout referencia (nome + preço).
PLANS = [
    {"name": "Essencial", "description": "Pra quem está começando.", "price": "49.90", "order": 1},
    {"name": "Profissional", "description": "O mais escolhido.", "price": "99.90", "order": 2},
    {"name": "Ilimitado", "description": "Sem limite de equipe.", "price": "179.90", "order": 3},
]


def seed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for data in PLANS:
        Plan.objects.get_or_create(
            name=data["name"],
            defaults={
                "description": data["description"],
                "price": data["price"],
                "order": data["order"],
                "is_active": True,
            },
        )


def remove_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(name__in=[p["name"] for p in PLANS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0004_subscription_trial_ends_at_alter_subscription_status_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_plans, remove_plans),
    ]
