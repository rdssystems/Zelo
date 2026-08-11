"""Agregações de Relatórios (RF37) — página unificada com 3 abas:

- **Visão Geral**: KPIs de hoje/mês e alertas operacionais (era `apps.dashboard`,
  incorporado aqui em 2026-08-05 pra não ter duas telas de "dashboard"
  redundantes — decisão do usuário).
- **Faturamento**: tendência + rankings (serviço/produto/funcionário) com
  período escolhido pelo usuário, não fixo.
- **DRE simplificado**: reaproveita `apps.finance.services.period_summary`
  direto, não duplicado aqui.
"""

import datetime
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate

from apps.clients.models import Client
from apps.finance.models import CashCategory, CashFlowType, CashTransaction, Commission, CommissionStatus
from apps.inventory.models import Product
from apps.scheduling.models import Appointment, AppointmentStatus


def month_bounds(today):
    return today.replace(day=1), today


# ---------------------------------------------------------------------------
# Visão Geral — KPIs de hoje/mês (janela fixa, sem filtro de período)
# ---------------------------------------------------------------------------


def revenue_kpis(tenant, today, month_start):
    month_qs = CashTransaction.objects.for_tenant(tenant).filter(
        created_at__date__range=(month_start, today)
    )
    today_qs = CashTransaction.objects.for_tenant(tenant).filter(created_at__date=today)

    def _sum(qs, flow_type):
        return qs.filter(type=flow_type).aggregate(total=Sum("amount"))["total"] or Decimal("0")

    month_in = _sum(month_qs, CashFlowType.IN)
    month_out = _sum(month_qs, CashFlowType.OUT)
    today_in = _sum(today_qs, CashFlowType.IN)
    today_out = _sum(today_qs, CashFlowType.OUT)
    return {
        "month_in": month_in,
        "month_out": month_out,
        "month_balance": month_in - month_out,
        "today_balance": today_in - today_out,
    }


def appointments_today(tenant, today):
    counts = dict(
        Appointment.objects.for_tenant(tenant)
        .filter(date=today)
        .values_list("status")
        .annotate(count=Count("id"))
    )
    return {status: counts.get(status, 0) for status, _ in AppointmentStatus.choices}


def pending_commissions_total(tenant):
    return Commission.objects.for_tenant(tenant).filter(
        status=CommissionStatus.PENDING
    ).aggregate(total=Sum("calculated_amount"))["total"] or Decimal("0")


def stock_kpis(tenant):
    products = Product.objects.for_tenant(tenant).filter(is_active=True)
    return {
        "low_stock_count": sum(1 for p in products if p.is_low_stock),
        "total_products": products.count(),
    }


def client_kpis(tenant, today, month_start):
    clients = Client.objects.for_tenant(tenant)
    return {
        "total_clients": clients.count(),
        "new_this_month": clients.filter(created_at__date__gte=month_start).count(),
        "subscribers_overdue": sum(1 for c in clients if c.subscription_is_overdue),
        "subscribers_due_soon": sum(1 for c in clients if c.subscription_is_due_soon),
        "credit_liability": clients.aggregate(total=Sum("credit_balance"))["total"] or Decimal("0"),
    }


def appointments_by_status_this_month(tenant, today, month_start):
    rows = (
        Appointment.objects.for_tenant(tenant)
        .filter(date__range=(month_start, today))
        .values_list("status")
        .annotate(count=Count("id"))
    )
    counts = dict(rows)
    labels = [label for value, label in AppointmentStatus.choices if counts.get(value)]
    values = [counts[value] for value, label in AppointmentStatus.choices if counts.get(value)]
    return labels, values


def commission_by_employee_this_month(tenant, today, month_start, limit=5):
    rows = (
        Commission.objects.for_tenant(tenant)
        .filter(created_at__date__range=(month_start, today))
        .values("employee__full_name")
        .annotate(total=Sum("calculated_amount"))
        .order_by("-total")[:limit]
    )
    return (
        [row["employee__full_name"] for row in rows],
        [float(row["total"]) for row in rows],
    )


# ---------------------------------------------------------------------------
# Faturamento — período escolhido pelo usuário
# ---------------------------------------------------------------------------


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
    """Ranking por faturamento (não por quantidade — a aba Visão Geral já
    mostra popularidade por contagem no gráfico de comissão; aqui o corte é
    o valor que cada serviço trouxe no período)."""
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
    comissão (aba Visão Geral) — aqui é quanto cada um trouxe de receita
    bruta pro salão."""
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


# ---------------------------------------------------------------------------
# Relatório semanal por e-mail — ver apps.reports.tasks.send_weekly_report_emails
# ---------------------------------------------------------------------------


def employees_weekly_summary(tenant, start_date, end_date):
    """Faturamento gerado (`Appointment.price_at_booking`, mesmo eixo de
    `revenue_by_employee`) e comissão (`Commission.calculated_amount`,
    mesmo eixo de `commission_by_employee_this_month`) de cada funcionário
    no período — pro e-mail semanal. Funcionário sem nenhum atendimento
    concluído no período simplesmente não aparece (times de salão são
    pequenos, sem necessidade de limitar quantidade)."""
    revenue_by_name = {
        row["employee__full_name"]: row["total"] or Decimal("0")
        for row in (
            Appointment.objects.for_tenant(tenant)
            .filter(status=AppointmentStatus.COMPLETED, date__range=(start_date, end_date))
            .values("employee__full_name")
            .annotate(total=Sum("price_at_booking"))
        )
    }
    commission_by_name = {
        row["employee__full_name"]: row["total"] or Decimal("0")
        for row in (
            Commission.objects.for_tenant(tenant)
            .filter(created_at__date__range=(start_date, end_date))
            .values("employee__full_name")
            .annotate(total=Sum("calculated_amount"))
        )
    }
    names = sorted(set(revenue_by_name) | set(commission_by_name))
    rows = [
        {
            "name": name,
            "revenue": revenue_by_name.get(name, Decimal("0")),
            "commission": commission_by_name.get(name, Decimal("0")),
        }
        for name in names
    ]
    rows.sort(key=lambda row: row["revenue"], reverse=True)
    return rows


def weekly_summary(tenant, start_date, end_date):
    """Resumo da semana (segunda a domingo) pro e-mail semanal — reaproveita
    as agregações já usadas em Relatórios/DRE, só com janela de 7 dias em
    vez de mês."""
    from apps.finance.services import period_summary

    cash = period_summary(tenant, start_date, end_date)
    completed_appointments = Appointment.objects.for_tenant(tenant).filter(
        status=AppointmentStatus.COMPLETED, date__range=(start_date, end_date)
    ).count()
    new_clients = Client.objects.for_tenant(tenant).filter(
        created_at__date__range=(start_date, end_date)
    ).count()
    service_labels, service_totals = top_services(tenant, start_date, end_date, limit=3)
    stock = stock_kpis(tenant)
    return {
        "revenue_in": cash["total_in"],
        "revenue_out": cash["total_out"],
        "balance": cash["balance"],
        "completed_appointments": completed_appointments,
        "new_clients": new_clients,
        "top_services": list(zip(service_labels, service_totals)),
        "employees": employees_weekly_summary(tenant, start_date, end_date),
        "low_stock_count": stock["low_stock_count"],
    }
