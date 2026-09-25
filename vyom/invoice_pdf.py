"""invoice_pdf -- renders a single-page GST invoice PDF for any of the
three payment ledgers (BusinessSubscription, FarmPlan, BusinessApiInvoice).
One renderer, fed a plain dict built by vyom/invoicing.py, so the layout
only ever lives in one place regardless of which product the invoice is
for.

Uses reportlab (pure-Python, no system deps) rather than an HTML->PDF
converter -- simpler deployment, and invoices are simple enough (one
issuer block, one bill-to block, one line-item table, one total) that
reportlab's Platypus layer is plenty.
"""
from datetime import datetime
from decimal import Decimal
from io import BytesIO

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT

from vyom.config import settings

_CANOPY = colors.HexColor("#3d7d49")
_TEXT_DIM = colors.HexColor("#6b756b")
_LINE = colors.HexColor("#e3e8e3")

# Same logo used in vyom/email_utils.py's HTML emails, reused here for
# visual consistency across every AgriDoot-branded document.
_LOGO_URL = "https://apiv2.agridoot.co.in:12443/img/app_img//AgriDoot_-_Logo_3_ed8bc3.png"
# Fetched once per process and cached -- invoices can be generated
# repeatedly (every Billing page load re-downloads its own PDF on click),
# and there's no reason to hit AgriDoot's image host every single time.
# None means "not fetched yet"; False means "fetch failed, don't retry
# this process" (the missing custom-port cert or a network hiccup isn't
# going to fix itself mid-process, and a logo is cosmetic -- worth failing
# quietly rather than slowing down or breaking invoice generation).
_logo_cache: bytes | None | bool = None


def _fetch_logo_bytes() -> bytes | None:
    global _logo_cache
    if _logo_cache is None:
        try:
            resp = requests.get(_LOGO_URL, timeout=5)
            resp.raise_for_status()
            _logo_cache = resp.content
        except requests.RequestException:
            _logo_cache = False
    return _logo_cache or None


def _rupees(paise: int) -> str:
    return f"Rs. {Decimal(paise) / 100:,.2f}"


def generate_invoice_pdf(
    *,
    invoice_number: str,
    issued_at: datetime,
    bill_to_name: str,
    bill_to_address: str | None,
    bill_to_gstin: str | None,
    bill_to_email: str | None,
    line_items: list[dict],   # [{"description": str, "amount_paise": int}]
    base_paise: int,
    gst_paise: int,
    total_paise: int,
    discount_paise: int = 0,
    payment_ref: str | None = None,
    payment_date: datetime | None = None,
    status: str = "paid",
) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=22 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "h1", parent=styles["Heading1"], fontSize=18, textColor=_CANOPY, spaceAfter=2)
    small_dim = ParagraphStyle(
        "small_dim", parent=styles["Normal"], fontSize=9, textColor=_TEXT_DIM, leading=13)
    normal = ParagraphStyle(
        "normal", parent=styles["Normal"], fontSize=10, leading=14)
    right_small = ParagraphStyle(
        "right_small", parent=small_dim, alignment=TA_RIGHT)

    story = []

    logo_bytes = _fetch_logo_bytes()
    if logo_bytes:
        logo_img = Image(BytesIO(logo_bytes), width=13 * mm, height=13 * mm)
        title_tbl = Table(
            [[logo_img, Paragraph("Vyom Engine", h1)]],
            colWidths=[16 * mm, 100 * mm],
        )
        title_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(title_tbl)
    else:
        story.append(Paragraph("Vyom Engine", h1))
    story.append(
        Paragraph("by AgriDoot &middot; Earth Observatory", small_dim))

    # -- Header block: issuer + invoice meta side by side --
    issuer_html = (
        f"<b>{settings.platform_legal_name}</b><br/>"
        f"{settings.platform_registered_address}<br/>"
        f"GSTIN: {settings.gst_number or 'N/A'}<br/>"
        f"{settings.platform_invoice_email}"
    )
    meta_html = (
        f"<b>INVOICE</b> #{invoice_number}<br/>"
        f"Issued: {issued_at.strftime('%d %b %Y')}<br/>"
        f"Status: {status.upper()}"
        + (f"<br/>Payment ref: {payment_ref}" if payment_ref else "")
    )
    header_tbl = Table(
        [[Paragraph(issuer_html, small_dim),
          Paragraph(meta_html, right_small)]],
        colWidths=[100 * mm, 70 * mm],
    )
    header_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(Spacer(1, 10 * mm))
    story.append(header_tbl)
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width="100%", color=_LINE, thickness=1))
    story.append(Spacer(1, 6 * mm))

    # -- Bill to --
    bill_html = f"<b>Bill to</b><br/>{bill_to_name}"
    if bill_to_address:
        bill_html += f"<br/>{bill_to_address}"
    if bill_to_gstin:
        bill_html += f"<br/>GSTIN: {bill_to_gstin}"
    if bill_to_email:
        bill_html += f"<br/>{bill_to_email}"
    story.append(Paragraph(bill_html, normal))
    story.append(Spacer(1, 8 * mm))

    # -- Line items --
    rows = [["Description", "Amount"]]
    for item in line_items:
        rows.append([item["description"], _rupees(item["amount_paise"])])
    if discount_paise:
        rows.append(["Discount / coupon", f"- {_rupees(discount_paise)}"])
    rows.append(["Subtotal", _rupees(base_paise)])
    rows.append([f"GST ({settings.gst_percent:g}%)", _rupees(gst_paise)])
    rows.append(["Total", _rupees(total_paise)])

    items_tbl = Table(rows, colWidths=[130 * mm, 40 * mm])
    n = len(rows)
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), _CANOPY),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, _LINE),
        ("LINEABOVE", (0, n - 1), (-1, n - 1), 1, _CANOPY),
        ("FONTNAME", (0, n - 1), (-1, n - 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, n - 1), (-1, n - 1), 11.5),
    ]
    for i in range(1, n - 1):
        style.append(("LINEBELOW", (0, i), (-1, i), 0.4, _LINE))
    items_tbl.setStyle(TableStyle(style))
    story.append(items_tbl)
    story.append(Spacer(1, 14 * mm))

    story.append(HRFlowable(width="100%", color=_LINE, thickness=1))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        "This is a system-generated invoice and does not require a signature. "
        "For billing questions, contact support@agridoot.com.",
        small_dim,
    ))

    doc.build(story)
    return buf.getvalue()
