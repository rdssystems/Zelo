from django.db import migrations, models

# Decisão do usuário em 2026-08-04: plano vira alavanca de quantos
# FUNCIONÁRIOS (contas de login) o tenant pode ter — não de dispositivo/sessão
# (ver conversa — dono usar PC + celular com o mesmo login continua livre).
# "Essencial" virou "Individual" (0 funcionário — só o dono, via "também
# atende" em Configurações); "Ilimitado" virou "Studio" porque deixou de ser
# ilimitado de verdade (agora até 6). Nomes/preço/ordem existentes não mudam.
PLAN_UPDATES = [
    {
        "old_name": "Essencial",
        "name": "Individual",
        "description": "Pra quem trabalha sozinho(a) — você atende, sem conta de funcionário extra.",
        "max_employees": 0,
    },
    {
        "old_name": "Profissional",
        "name": "Profissional",
        "description": "O mais escolhido — até 3 funcionários.",
        "max_employees": 3,
    },
    {
        "old_name": "Ilimitado",
        "name": "Studio",
        "description": "Pra equipes maiores — até 6 funcionários.",
        "max_employees": 6,
    },
]


def apply_plan_updates(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for data in PLAN_UPDATES:
        Plan.objects.filter(name=data["old_name"]).update(
            name=data["name"],
            description=data["description"],
            max_employees=data["max_employees"],
        )


def revert_plan_updates(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for data in PLAN_UPDATES:
        Plan.objects.filter(name=data["name"]).update(
            name=data["old_name"], max_employees=None
        )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0007_backfill_missing_trial_ends_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="max_employees",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text='Quantidade máxima de contas de funcionário (login próprio) '
                'permitida neste plano. O perfil do responsável usando "também atende" '
                "(Configurações) reaproveita o próprio login do dono e NUNCA conta aqui — "
                "ver apps.employees.models.Employee.is_owner. Vazio = sem limite.",
                null=True,
                verbose_name="limite de funcionários",
            ),
        ),
        migrations.RunPython(apply_plan_updates, revert_plan_updates),
    ]
