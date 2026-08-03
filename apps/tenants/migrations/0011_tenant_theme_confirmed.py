from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0010_tenant_theme"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="theme_confirmed",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "False só logo após um cadastro via Google (que não passa pelo formulário "
                    "de escolha de tema) — força a tela `painel/escolher-tema/` antes de liberar "
                    "o resto do painel (ver `apps.accounts.decorators.tenant_admin_required`). "
                    "Vira True assim que o dono confirma ali, ou sempre foi True pra quem já "
                    "escolheu no cadastro manual."
                ),
                verbose_name="tema confirmado pelo usuário",
            ),
        ),
    ]
