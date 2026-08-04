from django.conf import settings
from django.db import migrations


def set_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.filter(pk=settings.SITE_ID).update(domain="zellup.com.br", name="Zellup")


def revert_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.filter(pk=settings.SITE_ID).update(domain="example.com", name="example.com")


class Migration(migrations.Migration):
    """`django.contrib.sites` cria o Site padrão como "example.com" — sem
    corrigir isso, e-mails do allauth (confirmação, redefinição de senha)
    saem com prefixo de assunto "[example.com]" em vez de "[Zellup]"."""

    dependencies = [
        ("accounts", "0001_initial"),
        ("sites", "0002_alter_domain_unique"),
    ]

    operations = [
        migrations.RunPython(set_site_domain, revert_site_domain),
    ]
