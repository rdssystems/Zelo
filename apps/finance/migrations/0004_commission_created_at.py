import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0003_comandaproductitem"),
    ]

    operations = [
        migrations.AddField(
            model_name="commission",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AlterModelOptions(
            name="commission",
            options={
                "ordering": ["-created_at"],
                "verbose_name": "comissão",
                "verbose_name_plural": "comissões",
            },
        ),
    ]
