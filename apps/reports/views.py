import datetime
import json

from django.http import HttpResponse
from django.shortcuts import render

from apps.accounts.decorators import tenant_admin_required
from apps.finance.services import period_summary

from . import services as report_ops
from .pdf import generate_report_pdf


def _parse_date(raw, default):
    try:
        return datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        return default


@tenant_admin_required
def reports_view(request):
    today = datetime.date.today()
    month_start, _ = report_ops.month_bounds(today)
    start = _parse_date(request.GET.get("start"), month_start)
    end = _parse_date(request.GET.get("end"), today)

    # Visão Geral — janela fixa (hoje/mês), independente do filtro de período.
    status_labels, status_values = report_ops.appointments_by_status_this_month(
        request.tenant, today, month_start
    )
    commission_labels, commission_values = report_ops.commission_by_employee_this_month(
        request.tenant, today, month_start
    )

    # Faturamento — período escolhido pelo usuário.
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
            **report_ops.revenue_kpis(request.tenant, today, month_start),
            "appointments_today": report_ops.appointments_today(request.tenant, today),
            "pending_commissions_total": report_ops.pending_commissions_total(request.tenant),
            **report_ops.stock_kpis(request.tenant),
            **report_ops.client_kpis(request.tenant, today, month_start),
            "dre": period_summary(request.tenant, start, end),
            "status_chart_json": json.dumps({"labels": status_labels, "values": status_values}),
            "commission_chart_json": json.dumps(
                {"labels": commission_labels, "values": commission_values}
            ),
            "revenue_chart_json": json.dumps({"labels": revenue_labels, "values": revenue_values}),
            "services_chart_json": json.dumps({"labels": service_labels, "values": service_values}),
            "products_chart_json": json.dumps({"labels": product_labels, "values": product_values}),
            "employees_chart_json": json.dumps(
                {"labels": employee_labels, "values": employee_values}
            ),
        },
    )


@tenant_admin_required
def reports_pdf_view(request):
    """POST vindo do modal "Exportar PDF" — seções marcadas + o mesmo
    período (start/end) que estava filtrado na tela no momento do clique."""
    if request.method != "POST":
        return HttpResponse(status=405)

    today = datetime.date.today()
    month_start, _ = report_ops.month_bounds(today)
    start = _parse_date(request.POST.get("start"), month_start)
    end = _parse_date(request.POST.get("end"), today)
    sections = request.POST.getlist("sections") or ["visao_geral", "faturamento", "dre"]

    pdf_bytes = generate_report_pdf(
        tenant=request.tenant, sections=sections,
        today=today, month_start=month_start, start=start, end=end,
    )
    filename = f"relatorio-{request.tenant.slug}-{start.isoformat()}-a-{end.isoformat()}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
