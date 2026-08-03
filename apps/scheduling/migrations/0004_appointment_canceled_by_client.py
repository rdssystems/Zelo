from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0003_remove_appointment_unique_active_appointment_per_slot_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="canceled_by_client",
            field=models.BooleanField(
                default=False,
                help_text="True quando o próprio cliente cancelou pela página pública "
                "(RF06) — distingue de cancelamento feito pelo admin do salão na Agenda, "
                "pra exibir o aviso certo "
                "(`apps.scheduling.services.cancel_appointment`).",
                verbose_name="cancelado pelo cliente",
            ),
        ),
    ]
