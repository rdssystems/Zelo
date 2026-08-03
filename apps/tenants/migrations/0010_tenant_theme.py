from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0009_tenant_auto_confirm_appointments"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="theme",
            field=models.CharField(
                choices=[("salao", "Salão de beleza"), ("barbearia", "Barbearia")],
                default="salao",
                help_text="Escolhido no cadastro, editável em Configurações — muda a paleta e "
                "tipografia do app público e do painel.",
                max_length=20,
                verbose_name="tema visual",
            ),
        ),
    ]
