from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0006_tenant_document"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="subscription_due_soon_days",
            field=models.PositiveSmallIntegerField(
                default=7,
                help_text='Mensalista aparece como "vence em breve" quando faltar esse '
                "tanto de dias (ou menos) pro vencimento.",
                verbose_name="aviso de mensalidade a vencer (dias)",
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="client_inactive_days",
            field=models.PositiveSmallIntegerField(
                default=60,
                help_text="Sem nenhum atendimento concluído há esse tanto de dias (contado "
                "do cadastro se o cliente nunca voltou), aparece marcado como inativo na "
                "lista.",
                verbose_name="cliente inativo após (dias)",
            ),
        ),
    ]
