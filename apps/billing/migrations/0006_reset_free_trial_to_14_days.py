from django.db import migrations
from django.utils import timezone

# Pedido do usuário em 2026-07-31: reiniciar a contagem dos 14 dias grátis de
# toda assinatura ainda no plano gratuito (sem `plan`, em teste) a partir de
# hoje — não mexe em quem já assinou um plano pago.
TRIAL_DAYS = 14


def reset_trial(apps, schema_editor):
    Subscription = apps.get_model("billing", "Subscription")
    Subscription.objects.filter(plan__isnull=True, status="trialing").update(
        trial_ends_at=timezone.now() + timezone.timedelta(days=TRIAL_DAYS)
    )


def noop(apps, schema_editor):
    # Não dá pra "desfazer" um reset de contagem regressiva com segurança —
    # a migration reversa é intencionalmente um no-op.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0005_seed_plans"),
    ]

    operations = [
        migrations.RunPython(reset_trial, noop),
    ]
