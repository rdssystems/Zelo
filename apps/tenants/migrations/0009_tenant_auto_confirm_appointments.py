from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0008_tenant_whatsapp_cancel_redirect"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="auto_confirm_appointments",
            field=models.BooleanField(
                default=False,
                help_text="Desmarcado (padrão): todo agendamento nasce pendente, precisa o "
                "salão confirmar na Agenda — a página do cliente mostra \"Agendamento "
                "enviado\" até lá. Marcado: agendamento já nasce confirmado, sem esperar o "
                "salão.",
                verbose_name="confirmar agendamento automaticamente",
            ),
        ),
    ]
