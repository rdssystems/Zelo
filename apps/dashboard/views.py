"""Visão geral do salão — agrega dados de todos os apps (agenda, caixa,
comissão, estoque, clientes). Só views, sem models próprios: cada métrica
reaproveita a lógica de agregação que já existe em cada app (mesmo espírito
de `apps/finance/services.py::period_summary` e
`apps/inventory/views.py::_stock_stats`, sem duplicar cálculo)."""

import datetime
import json
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import render

from apps.accounts.decorators import tenant_admin_required
from apps.clients.models import Client
from apps.finance.models import CashFlowType, CashTransaction, Commission, CommissionStatus
from apps.inventory.models import Product
from apps.scheduling.models import Appointment, AppointmentStatus

CHART_WINDOW_DAYS = 30


def _month_bounds(today):
    start = today.replace(day=1)
    return start, today


def _revenue_kpis(tenant, today, month_start):
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


def _appointments_today(tenant, today):
    counts = dict(
        Appointment.objects.for_tenant(tenant)
        .filter(date=today)
        .values_list("status")
        .annotate(count=Count("id"))
    )
    return {status: counts.get(status, 0) for status, _ in AppointmentStatus.choices}


def _pending_commissions_total(tenant):
    return Commission.objects.for_tenant(tenant).filter(
        status=CommissionStatus.PENDING
    ).aggregate(total=Sum("calculated_amount"))["total"] or Decimal("0")


def _stock_kpis(tenant):
    products = Product.objects.for_tenant(tenant).filter(is_active=True)
    return {
        "low_stock_count": sum(1 for p in products if p.is_low_stock),
        "total_products": products.count(),
    }


def _client_kpis(tenant, today, month_start):
    clients = Client.objects.for_tenant(tenant)
    return {
        "total_clients": clients.count(),
        "new_this_month": clients.filter(created_at__date__gte=month_start).count(),
        "subscribers_overdue": sum(1 for c in clients if c.subscription_is_overdue),
        "subscribers_due_soon": sum(1 for c in clients if c.subscription_is_due_soon),
        "credit_liability": clients.aggregate(total=Sum("credit_balance"))["total"] or Decimal("0"),
    }


def _revenue_trend(tenant, today):
    start = today - datetime.timedelta(days=CHART_WINDOW_DAYS - 1)
    rows = (
        CashTransaction.objects.for_tenant(tenant)
        .filter(type=CashFlowType.IN, created_at__date__range=(start, today))
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Sum("amount"))
        .order_by("day")
    )
    totals_by_day = {row["day"]: row["total"] for row in rows}
    labels, values = [], []
    for i in range(CHART_WINDOW_DAYS):
        day = start + datetime.timedelta(days=i)
        labels.append(day.strftime("%d/%m"))
        values.append(float(totals_by_day.get(day, Decimal("0"))))
    return labels, values


def _appointments_by_status_this_month(tenant, today, month_start):
    rows = (
        Appointment.objects.for_tenant(tenant)
        .filter(date__range=(month_start, today))
        .values_list("status")
        .annotate(count=Count("id"))
    )
    counts = dict(rows)
    labels_map = dict(AppointmentStatus.choices)
    labels = [label for value, label in AppointmentStatus.choices if counts.get(value)]
    values = [counts[value] for value, label in AppointmentStatus.choices if counts.get(value)]
    return labels, values


def _top_services(tenant, today, month_start, limit=5):
    rows = (
        Appointment.objects.for_tenant(tenant)
        .filter(status=AppointmentStatus.COMPLETED, date__range=(month_start, today))
        .values("service__name")
        .annotate(count=Count("id"))
        .order_by("-count")[:limit]
    )
    return [row["service__name"] for row in rows], [row["count"] for row in rows]


def _commission_by_employee(tenant, today, month_start, limit=5):
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


@tenant_admin_required
def dashboard_view(request):
    tenant = request.tenant
    today = datetime.date.today()
    month_start, _ = _month_bounds(today)

    revenue_labels, revenue_values = _revenue_trend(tenant, today)
    status_labels, status_values = _appointments_by_status_this_month(tenant, today, month_start)
    service_labels, service_values = _top_services(tenant, today, month_start)
    employee_labels, employee_values = _commission_by_employee(tenant, today, month_start)

    context = {
        "active_nav": "dashboard",
        "today": today,
        **_revenue_kpis(tenant, today, month_start),
        "appointments_today": _appointments_today(tenant, today),
        "pending_commissions_total": _pending_commissions_total(tenant),
        **_stock_kpis(tenant),
        **_client_kpis(tenant, today, month_start),
        "revenue_chart_json": json.dumps({"labels": revenue_labels, "values": revenue_values}),
        "status_chart_json": json.dumps({"labels": status_labels, "values": status_values}),
        "services_chart_json": json.dumps({"labels": service_labels, "values": service_values}),
        "employees_chart_json": json.dumps({"labels": employee_labels, "values": employee_values}),
    }
    return render(request, "painel/dashboard/index.html", context)
