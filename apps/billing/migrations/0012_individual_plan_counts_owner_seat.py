from django.db import migrations

# Decisão do usuário em 2026-08-07: o dono passou a ocupar vaga do plano
# igual um funcionário contratado quando "também atende" está ligado (ver
# apps.billing.services.employee_seats_used) — revertendo a decisão de
# 2026-08-04 de nunca contá-lo. "Individual" era `max_employees=0` só porque
# o dono nunca contava; com ele contando, o plano "só você atende" precisa de
# 1 vaga (a dele), senão o próprio dono não consegue ligar "também atende"
# no plano feito exatamente pra esse caso. Profissional (3) e Studio (6) não
# mudam de número — já eram pensados como "pessoas atendendo no total".
OLD_INDIVIDUAL_MAX = 0
NEW_INDIVIDUAL_MAX = 1


def apply_bump(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(name="Individual", max_employees=OLD_INDIVIDUAL_MAX).update(
        max_employees=NEW_INDIVIDUAL_MAX
    )


def revert_bump(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(name="Individual", max_employees=NEW_INDIVIDUAL_MAX).update(
        max_employees=OLD_INDIVIDUAL_MAX
    )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0011_alter_plan_max_employees"),
    ]

    operations = [
        migrations.RunPython(apply_bump, revert_bump),
    ]
