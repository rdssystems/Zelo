"""Agregações do relatório com período flexível (RF37).

O resumo por categoria (DRE simplificado) já existe pronto em
`apps.finance.services.period_summary` — não duplicado aqui, a view importa
direto de lá. Este módulo cobre só o que ainda faltava: tendência de
faturamento, top serviços/produtos/funcionários por período escolhido pelo
usuário (o dashboard já tem equivalentes, mas com janela fixa de 30 dias/mês
atual — ver `apps/dashboard/views.py`).
"""

import datetime
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import TruncDate

from apps.finance.models import CashCategory, CashFlowType, CashTransaction
from apps.scheduling.models import Appointment, AppointmentStatus


def revenue_trend(tenant, start_date, end_date):
    rows = (
        CashTransaction.objects.for_tenant(tenant)
        .filter(type=CashFlowType.IN, created_at__date__range=(start_date, end_date))
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Sum("amount"))
        .order_by("day")
    )
    totals_by_day = {row["day"]: row["total"] for row in rows}
    labels, values = [], []
    day = start_date
    while day <= end_date:
        labels.append(day.strftime("%d/%m"))
        values.append(float(totals_by_day.get(day, Decimal("0"))))
        day += datetime.timedelta(days=1)
    return labels, values


def top_services(tenant, start_date, end_date, limit=10):
    """Ranking por faturamento (não por quantidade — o dashboard já mostra
    popularidade por contagem; aqui o corte é o valor que cada serviço
    trouxe no período)."""
    rows = (
        Appointment.objects.for_tenant(tenant)
        .filter(status=AppointmentStatus.COMPLETED, date__range=(start_date, end_date))
        .values("service__name")
        .annotate(total=Sum("price_at_booking"))
        .order_by("-total")[:limit]
    )
    return (
        [row["service__name"] for row in rows],
        [float(row["total"] or 0) for row in rows],
    )


def top_products(tenant, start_date, end_date, limit=10):
    rows = (
        CashTransaction.objects.for_tenant(tenant)
        .filter(category=CashCategory.PRODUCT_SALE, created_at__date__range=(start_date, end_date))
        .values("related_stock_movement__product__name")
        .annotate(total=Sum("amount"))
        .order_by("-total")[:limit]
    )
    return (
        [row["related_stock_movement__product__name"] for row in rows],
        [float(row["total"] or 0) for row in rows],
    )


def revenue_by_employee(tenant, start_date, end_date, limit=10):
    """Faturamento gerado por funcionário (valor do serviço), diferente da
    comissão (que já aparece no dashboard) — aqui é quanto cada um trouxe
    de receita bruta pro salão."""
    rows = (
        Appointment.objects.for_tenant(tenant)
        .filter(status=AppointmentStatus.COMPLETED, date__range=(start_date, end_date))
        .values("employee__full_name")
        .annotate(total=Sum("price_at_booking"))
        .order_by("-total")[:limit]
    )
    return (
        [row["employee__full_name"] for row in rows],
        [float(row["total"] or 0) for row in rows],
    )
