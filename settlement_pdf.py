"""Renders a settlement's numbers as a real PDF (2026-09-25, Broc: "we
still add the settlement sheet as a pdf") -- the one place that turns a
settlement into a document; app.py calls this and files the result the
same way it files any other show attachment.
"""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _money(n):
    return f"${float(n):,.2f}" if n is not None else "—"


def generate(show_title, venue_name, show_date, deal_type, guarantee, backend_pct,
             tickets_sold, gross, net_gross, expense_lines, expenses_total,
             net_after_expenses, artist_payout, settled):
    """Returns PDF bytes. expense_lines: list of (label, actual)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.75 * inch, bottomMargin=0.75 * inch)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"Settlement — {show_title}", styles["Title"]))
    story.append(Paragraph(f"{venue_name} · {show_date}", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    deal_line = deal_type or "Not recorded"
    if guarantee:
        deal_line += f" · Guarantee {_money(guarantee)}"
    if backend_pct:
        deal_line += f" · {backend_pct}%"
    story.append(Paragraph(f"<b>Deal:</b> {deal_line}", styles["Normal"]))
    story.append(Spacer(1, 0.15 * inch))

    summary_rows = [
        ["Tickets sold", str(tickets_sold) if tickets_sold is not None else "—"],
        ["Gross", _money(gross)],
        ["Net gross (after tax/fees)", _money(net_gross)],
        ["Total expenses", _money(expenses_total)],
        ["Net after expenses", _money(net_after_expenses)],
        ["Artist payout", _money(artist_payout)],
        ["Settled", "Yes" if settled else "No"],
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

    if expense_lines:
        story.append(Paragraph("Expenses", styles["Heading3"]))
        expense_rows = [["Label", "Amount"]] + [[label, _money(actual)] for label, actual in expense_lines]
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
