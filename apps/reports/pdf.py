"""Geração do relatório em PDF (RF37) — ReportLab (Python puro, sem
dependência de sistema no Docker, ao contrário de renderizadores HTML→PDF
tipo WeasyPrint — decisão tomada com o usuário em 2026-08-05).

Reaproveita as mesmas agregações de `apps.reports.services` e
`apps.finance.services.period_summary` que alimentam a página — o PDF nunca
recalcula nada diferente do que está na tela, só formata pra documento.
"""

import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.finance.services import period_summary

from . import services as report_ops

BRAND_COLOR = colors.HexColor("#7d562d")
LIGHT_BG = colors.HexColor("#f5efe6")
LINE_COLOR = colors.HexColor("#d9d0c5")


def _money(value):
    return f"R$ {value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        "SectionTitle", parent=styles["Heading2"], textColor=BRAND_COLOR,
        spaceBefore=18, spaceAfter=8,
    ))
    return styles


def _table(data, col_widths=None, header=True):
    table = Table(data, colWidths=col_widths, hAlign="LEFT")
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE_COLOR),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BG),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
    table.setStyle(TableStyle(style))
    return table


def generate_report_pdf(*, tenant, sections, today, month_start, start, end):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
        title=f"Relatório — {tenant.name}",
        # Sem compressão: PDF de relatório é pequeno (só texto/tabela), e
        # deixar sem FlateDecode facilita inspecionar/testar o conteúdo.
        pageCompression=0,
    )
    styles = _styles()
    story = [
        Paragraph(f"Relatório — {tenant.name}", styles["Title"]),
        Paragraph(
            f"Gerado em {today.strftime('%d/%m/%Y')} · Faturamento/DRE do período "
            f"{start.strftime('%d/%m/%Y')} a {end.strftime('%d/%m/%Y')}",
            styles["Normal"],
        ),
        Spacer(1, 0.6 * cm),
    ]

    if "visao_geral" in sections:
        story += _visao_geral_section(tenant, today, month_start, styles)
    if "faturamento" in sections:
        story += _faturamento_section(tenant, start, end, styles)
    if "dre" in sections:
        story += _dre_section(tenant, start, end, styles)

    doc.build(story)
    return buffer.getvalue()


def _visao_geral_section(tenant, today, month_start, styles):
    story = [Paragraph("Visão Geral", styles["SectionTitle"])]

    kpis = report_ops.revenue_kpis(tenant, today, month_start)
    stock = report_ops.stock_kpis(tenant)
    clients = report_ops.client_kpis(tenant, today, month_start)
    appts = report_ops.appointments_today(tenant, today)

    kpi_rows = [
        ["Faturamento do mês", _money(kpis["month_in"])],
        ["Saldo do caixa hoje", _money(kpis["today_balance"])],
        ["Comissões pendentes", _money(report_ops.pending_commissions_total(tenant))],
        ["Crédito de clientes em aberto", _money(clients["credit_liability"])],
        ["Clientes (total / novos no mês)", f"{clients['total_clients']} / {clients['new_this_month']}"],
        ["Produtos ativos (estoque baixo)", f"{stock['total_products']} ({stock['low_stock_count']})"],
    ]
    story.append(_table(kpi_rows, col_widths=[9 * cm, 6 * cm], header=False))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Atendimentos hoje", styles["Heading4"]))
    appt_rows = [
        ["Status", "Quantidade"],
        ["Pendentes", appts.get("pending", 0)],
        ["Confirmados", appts.get("confirmed", 0)],
        ["Em atendimento", appts.get("in_progress", 0)],
        ["Concluídos", appts.get("completed", 0)],
    ]
    story.append(_table(appt_rows, col_widths=[9 * cm, 6 * cm]))

    labels, values = report_ops.commission_by_employee_this_month(tenant, today, month_start, limit=10)
    if labels:
        story.append(Spacer(1, 0.4 * cm))
        story.append(Paragraph("Comissão por funcionário (mês)", styles["Heading4"]))
        rows = [["Funcionário", "Comissão"]] + [
            [label, _money(value)] for label, value in zip(labels, values)
        ]
        story.append(_table(rows, col_widths=[9 * cm, 6 * cm]))

    return story


def _faturamento_section(tenant, start, end, styles):
    story = [Paragraph("Faturamento", styles["SectionTitle"])]

    _, revenue_values = report_ops.revenue_trend(tenant, start, end)
    story.append(Paragraph(f"Faturamento total no período: {_money(sum(revenue_values))}", styles["Normal"]))
    story.append(Spacer(1, 0.3 * cm))

    rankings = (
        ("Serviços — por faturamento", report_ops.top_services(tenant, start, end)),
        ("Produtos mais vendidos", report_ops.top_products(tenant, start, end)),
        ("Faturamento por funcionário", report_ops.revenue_by_employee(tenant, start, end)),
    )
    for title, (item_labels, item_values) in rankings:
        story.append(Paragraph(title, styles["Heading4"]))
        if item_labels:
            rows = [["Nome", "Total"]] + [
                [label, _money(value)] for label, value in zip(item_labels, item_values)
            ]
            story.append(_table(rows, col_widths=[9 * cm, 6 * cm]))
        else:
            story.append(Paragraph("Sem dados no período.", styles["Normal"]))
        story.append(Spacer(1, 0.4 * cm))

    return story


def _dre_section(tenant, start, end, styles):
    story = [Paragraph("DRE simplificado", styles["SectionTitle"])]

    summary = period_summary(tenant, start, end)
    totals = [
        ["Entradas", _money(summary["total_in"])],
        ["Saídas", _money(summary["total_out"])],
        ["Saldo do período", _money(summary["balance"])],
    ]
    story.append(_table(totals, col_widths=[9 * cm, 6 * cm], header=False))
    story.append(Spacer(1, 0.4 * cm))

    if summary["by_category"]:
        rows = [["Categoria", "Total"]] + [
            [row["category"], _money(row["total"])] for row in summary["by_category"]
        ]
        story.append(_table(rows, col_widths=[9 * cm, 6 * cm]))

    return story
