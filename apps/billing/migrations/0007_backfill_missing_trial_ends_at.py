from django.db import migrations
from django.utils import timezone

# Achado em 2026-07-31: assinatura "Catherine's Sthetic" (status pending, plano
# Ilimitado escolhido mas pagamento nunca confirmado) tinha `trial_ends_at`
# nulo — nunca passou pela contagem de 14 dias, então o painel não tinha o que
# mostrar. A 0006 só resetou quem já tinha `trial_ends_at` e nenhum plano
# (`plan__isnull=True`); esta migration cobre o caso que passou batido: toda
# assinatura que ainda não está paga (`status != active`) mas está sem
# `trial_ends_at` ganha os 14 dias corridos a partir de hoje.
TRIAL_DAYS = 14


def backfill_trial_ends_at(apps, schema_editor):
    Subscription = apps.get_model("billing", "Subscription")
    Subscription.objects.filter(trial_ends_at__isnull=True).exclude(status="active").update(
        trial_ends_at=timezone.now() + timezone.timedelta(days=TRIAL_DAYS)
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0006_reset_free_trial_to_14_days"),
    ]

    operations = [
        migrations.RunPython(backfill_trial_ends_at, noop),
    ]
