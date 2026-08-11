"""Tasks periódicas de Relatórios — agendadas via `CELERY_BEAT_SCHEDULE`
(`config/settings.py`)."""

import datetime
import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from apps.tenants.models import Tenant

from .services import weekly_summary

logger = logging.getLogger(__name__)

User = get_user_model()


def _previous_week_bounds(today=None):
    """Segunda a domingo da semana ANTERIOR (fechada) — nunca a semana
    corrente, pra sempre mandar um período completo."""
    today = today or datetime.date.today()
    this_monday = today - datetime.timedelta(days=today.weekday())
    start = this_monday - datetime.timedelta(days=7)
    end = start + datetime.timedelta(days=6)
    return start, end


def _send_weekly_report_email(tenant, start, end):
    recipients = list(
        User.objects.filter(tenant=tenant, role=User.Role.TENANT_ADMIN, is_active=True)
        .values_list("email", flat=True)
    )
    if not recipients:
        return

    summary = weekly_summary(tenant, start, end)
    html = render_to_string(
        "emails/weekly_report.html",
        {"tenant": tenant, "start": start, "end": end, **summary},
    )
    message = EmailMultiAlternatives(
        subject=f"Resumo da semana — {tenant.name}",
        body=strip_tags(html),
        from_email=None,
        to=recipients,
    )
    message.attach_alternative(html, "text/html")
    message.send(fail_silently=False)


@shared_task
def send_weekly_report_emails():
    """RF — relatório semanal por e-mail (decisão do usuário: opt-out, liga
    por padrão). Roda toda segunda de manhã (`CELERY_BEAT_SCHEDULE`); falha
    ao enviar pra UM tenant não pode travar o envio pros demais."""
    start, end = _previous_week_bounds()
    tenants = Tenant.objects.filter(is_active=True, weekly_report_email_enabled=True)
    for tenant in tenants:
        try:
            _send_weekly_report_email(tenant, start, end)
        except Exception:
            logger.exception("Falha ao enviar relatório semanal pro tenant %s", tenant.slug)
