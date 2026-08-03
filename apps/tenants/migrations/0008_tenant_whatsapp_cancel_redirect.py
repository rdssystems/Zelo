from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0007_tenant_engagement_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="whatsapp_cancel_redirect_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Ao cancelar um agendamento na página pública, abre o WhatsApp do "
                "salão com um aviso pronto (editável) pro cliente só clicar em enviar. Exige "
                "WhatsApp cadastrado acima.",
                verbose_name="redirecionar cliente pro WhatsApp ao cancelar",
            ),
        ),
    ]
