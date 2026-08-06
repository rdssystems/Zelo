"""Geração do relatório em PDF (RF37) — ReportLab (Python puro, sem
dependência de sistema no Docker, ao contrário de renderizadores HTML→PDF
tipo WeasyPrint — decisão tomada com o usuário em 2026-08-05).

Reaproveita as mesmas agregações de `apps.reports.services` e
`apps.finance.services.dre_breakdown` que alimentam a página — o PDF nunca
recalcula nada diferente do que está na tela, só formata pra documento.
"""

import io

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.finance.services import dre_breakdown

from . import services as report_ops

BRAND_COLOR = colors.HexColor("#7d562d")
LIGHT_BG = colors.HexColor("#f5efe6")
LINE_COLOR = colors.HexColor("#d9d0c5")

# Identidade visual oficial (2026-08-06, pasta "zellup identidade visual"
# fornecida pelo usuário) — monograma processado em static/img/brand/.
ZELLUP_ICON_PATH = settings.BASE_DIR / "static" / "img" / "brand" / "zellup-mark.png"

HEADER_TOP_MARGIN = 3.2 * cm
FOOTER_BOTTOM_MARGIN = 2.2 * cm


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


def _draw_image_safe(canvas, path, x, y, size):
    """Desenha um quadrado `size`x`size` em `(x, y)` — silenciosamente não
    desenha nada se o arquivo não existir/não abrir (ex: logo do tenant foi
    removido do disco por fora do app). Devolve se desenhou ou não, pra quem
    chama decidir o layout do texto ao lado."""
    try:
        canvas.drawImage(
            str(path), x, y, width=size, height=size,
            preserveAspectRatio=True, anchor="sw", mask="auto",
        )
        return True
    except Exception:
        return False


def _make_header_footer(tenant):
    tenant_logo_path = None
    if tenant.logo:
        try:
            tenant_logo_path = tenant.logo.path
        except Exception:
            tenant_logo_path = None

    def draw(canvas, doc):
        canvas.saveState()
        width, height = A4
        logo_size = 1.1 * cm
        logo_y = height - 1.9 * cm
        text_baseline = height - 1.4 * cm

        # Esquerda — ícone + nome do Zellup (plataforma).
        left_x = 2 * cm
        has_zellup_icon = _draw_image_safe(canvas, ZELLUP_ICON_PATH, left_x, logo_y, logo_size)
        text_x = left_x + (logo_size + 0.3 * cm if has_zellup_icon else 0)
        canvas.setFont("Helvetica-Bold", 12)
        canvas.setFillColor(BRAND_COLOR)
        canvas.drawString(text_x, text_baseline, "Zellup")

        # Direita — nome do salão/barbearia + logo dele (se tiver).
        right_edge = width - 2 * cm
        canvas.setFont("Helvetica-Bold", 12)
        canvas.setFillColor(colors.black)
        name_width = canvas.stringWidth(tenant.name, "Helvetica-Bold", 12)
        name_x = right_edge - name_width
        if tenant_logo_path:
            logo_x = name_x - 0.3 * cm - logo_size
            if _draw_image_safe(canvas, tenant_logo_path, logo_x, logo_y, logo_size):
                name_x = name_x  # nome fica encostado no logo, à direita dele
        canvas.drawString(name_x, text_baseline, tenant.name)

        canvas.setStrokeColor(LINE_COLOR)
        canvas.line(2 * cm, height - 2.3 * cm, width - 2 * cm, height - 2.3 * cm)

        # Rodapé.
        canvas.setStrokeColor(LINE_COLOR)
        canvas.line(2 * cm, FOOTER_BOTTOM_MARGIN - 0.3 * cm, width - 2 * cm, FOOTER_BOTTOM_MARGIN - 0.3 * cm)
        canvas.setFont("Helvetica-Oblique", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(
            2 * cm, FOOTER_BOTTOM_MARGIN - 0.7 * cm,
            "Arquivo gerado automaticamente pela plataforma Zellup — zellup.com.br",
        )
        canvas.drawRightString(
            width - 2 * cm, FOOTER_BOTTOM_MARGIN - 0.7 * cm, f"Página {canvas.getPageNumber()}"
        )

        canvas.restoreState()

    return draw


def generate_report_pdf(*, tenant, sections, today, month_start, start, end):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=HEADER_TOP_MARGIN, bottomMargin=FOOTER_BOTTOM_MARGIN,
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

    header_footer = _make_header_footer(tenant)
    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    return buffer.getvalue()


def _visao_geral_section(tenant, today, month_start, styles):
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
    appt_rows = [
        ["Status", "Quantidade"],
        ["Pendentes", appts.get("pending", 0)],
        ["Confirmados", appts.get("confirmed", 0)],
        ["Em atendimento", appts.get("in_progress", 0)],
        ["Concluídos", appts.get("completed", 0)],
    ]

    # KeepTogether em cada título+tabela — sem isso o ReportLab pode quebrar
    # a página bem entre o título e a tabela (título sozinho no fim de uma
    # página, tabela começando na próxima) — decisão do usuário em 2026-08-06.
    story = [
        KeepTogether([
            Paragraph("Visão Geral", styles["SectionTitle"]),
            _table(kpi_rows, col_widths=[9 * cm, 6 * cm], header=False),
        ]),
        Spacer(1, 0.4 * cm),
        KeepTogether([
            Paragraph("Atendimentos hoje", styles["Heading4"]),
            _table(appt_rows, col_widths=[9 * cm, 6 * cm]),
        ]),
    ]

    labels, values = report_ops.commission_by_employee_this_month(tenant, today, month_start, limit=10)
    if labels:
        rows = [["Funcionário", "Comissão"]] + [
            [label, _money(value)] for label, value in zip(labels, values)
        ]
        story.append(Spacer(1, 0.4 * cm))
        story.append(KeepTogether([
            Paragraph("Comissão por funcionário (mês)", styles["Heading4"]),
            _table(rows, col_widths=[9 * cm, 6 * cm]),
        ]))

    return story


def _faturamento_section(tenant, start, end, styles):
    _, revenue_values = report_ops.revenue_trend(tenant, start, end)

    story = [
        KeepTogether([
            Paragraph("Faturamento", styles["SectionTitle"]),
            Paragraph(
                f"Faturamento total no período: {_money(sum(revenue_values))}", styles["Normal"]
            ),
        ]),
        Spacer(1, 0.3 * cm),
    ]

    rankings = (
        ("Serviços — por faturamento", report_ops.top_services(tenant, start, end)),
        ("Produtos mais vendidos", report_ops.top_products(tenant, start, end)),
        ("Faturamento por funcionário", report_ops.revenue_by_employee(tenant, start, end)),
    )
    for title, (item_labels, item_values) in rankings:
        if item_labels:
            rows = [["Nome", "Total"]] + [
                [label, _money(value)] for label, value in zip(item_labels, item_values)
            ]
            content = _table(rows, col_widths=[9 * cm, 6 * cm])
        else:
            content = Paragraph("Sem dados no período.", styles["Normal"])
        story.append(KeepTogether([Paragraph(title, styles["Heading4"]), content]))
        story.append(Spacer(1, 0.4 * cm))

    return story


def _dre_section(tenant, start, end, styles):
    dre = dre_breakdown(tenant, start, end)

    cascade_rows = [
        ["Receita", _money(dre["revenue"])],
        ["(-) Custo direto (comissão)", _money(dre["direct_cost"])],
        ["= Margem de contribuição", _money(dre["contribution_margin"])],
        ["(-) Despesas fixas", _money(dre["fixed_total"])],
        ["(-) Despesas variáveis", _money(dre["variable_total"])],
    ]
    if dre["uncategorized_total"]:
        cascade_rows.append(["(-) Despesas sem categoria", _money(dre["uncategorized_total"])])
    if dre["other_out"]:
        cascade_rows.append(["(-) Outras saídas", _money(dre["other_out"])])
    cascade_rows.append(["= Resultado do período", _money(dre["result"])])

    story = [
        KeepTogether([
            Paragraph("DRE simplificado", styles["SectionTitle"]),
            _table(cascade_rows, col_widths=[9 * cm, 6 * cm], header=False),
        ]),
    ]

    for title, by_category in (
        ("Despesas fixas por categoria", dre["fixed_by_category"]),
        ("Despesas variáveis por categoria", dre["variable_by_category"]),
    ):
        if not by_category:
            continue
        rows = [["Categoria", "Total"]] + [
            [row["name"], _money(row["total"])] for row in by_category
        ]
        story.append(Spacer(1, 0.4 * cm))
        story.append(KeepTogether([
            Paragraph(title, styles["Heading4"]),
            _table(rows, col_widths=[9 * cm, 6 * cm]),
        ]))

    return story
