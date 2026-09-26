"""Renders an offer's numbers as a real PDF (2026-09-26, Broc: "we need
both the offer and the settlement sheets to save as pdf so we can send
them to agents") -- same reportlab platypus approach as settlement_pdf.py,
deliberately kept as a separate module since an offer and a settlement are
different documents (projected capacity/possible-gross vs. real sold/
actual), not two views of the same data.
"""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _money(n):
    return f"${float(n):,.2f}" if n is not None else "—"


def generate(title, venue_name, artist_name, deal_type, guarantee, backend_pct,
             expense_lines, expenses_total, tier_lines, total_capacity, possible_gross):
    """Returns PDF bytes. expense_lines: list of (label, budget). tier_lines:
    list of (label, price, capacity)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.75 * inch, bottomMargin=0.75 * inch)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"Offer — {title}", styles["Title"]))
    subtitle = " · ".join(x for x in (venue_name, artist_name) if x)
    if subtitle:
        story.append(Paragraph(subtitle, styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    deal_line = deal_type or "Not recorded"
    if guarantee:
        deal_line += f" · Guarantee {_money(guarantee)}"
    if backend_pct:
        deal_line += f" · {backend_pct}%"
    story.append(Paragraph(f"<b>Deal:</b> {deal_line}", styles["Normal"]))
    story.append(Spacer(1, 0.15 * inch))

    summary_rows = [
        ["Total capacity", str(total_capacity) if total_capacity is not None else "—"],
        ["Possible gross (at sellout)", _money(possible_gross)],
        ["Total budgeted expenses", _money(expenses_total)],
    ]
    summary_table = Table(summary_rows, colWidths=[3 * inch, 2.5 * inch])
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.25 * inch))

    if tier_lines:
        story.append(Paragraph("Ticket Tiers", styles["Heading3"]))
        tier_rows = [["Tier", "Price", "Capacity", "Gross at sellout"]] + [
            [label, _money(price), str(capacity) if capacity is not None else "—",
             _money((price or 0) * (capacity or 0))]
            for label, price, capacity in tier_lines
        ]
        tier_table = Table(tier_rows, colWidths=[2.25 * inch, 1.25 * inch, 1.25 * inch, 1.75 * inch])
        tier_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.lightgrey),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(tier_table)
        story.append(Spacer(1, 0.25 * inch))

    if expense_lines:
        story.append(Paragraph("Budgeted Expenses", styles["Heading3"]))
        expense_rows = [["Label", "Budget"]] + [[label, _money(budget)] for label, budget in expense_lines]
        expense_table = Table(expense_rows, colWidths=[3.5 * inch, 2 * inch])
        expense_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.lightgrey),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(expense_table)

    doc.build(story)
    return buf.getvalue()
