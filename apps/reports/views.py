import datetime
import json

from django.shortcuts import render

from apps.accounts.decorators import tenant_admin_required
from apps.finance.services import period_summary

from . import services as report_ops


def _parse_date(raw, default):
    try:
        return datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        return default


@tenant_admin_required
def reports_view(request):
    today = datetime.date.today()
    start = _parse_date(request.GET.get("start"), today.replace(day=1))
    end = _parse_date(request.GET.get("end"), today)

    revenue_labels, revenue_values = report_ops.revenue_trend(request.tenant, start, end)
    service_labels, service_values = report_ops.top_services(request.tenant, start, end)
    product_labels, product_values = report_ops.top_products(request.tenant, start, end)
    employee_labels, employee_values = report_ops.revenue_by_employee(request.tenant, start, end)

    return render(
        request,
        "painel/reports/index.html",
        {
            "active_nav": "reports",
            "start": start,
            "end": end,
            "today": today,
            "dre": period_summary(request.tenant, start, end),
            "revenue_chart_json": json.dumps({"labels": revenue_labels, "values": revenue_values}),
            "services_chart_json": json.dumps({"labels": service_labels, "values": service_values}),
            "products_chart_json": json.dumps({"labels": product_labels, "values": product_values}),
            "employees_chart_json": json.dumps({"labels": employee_labels, "values": employee_values}),
        },
    )
