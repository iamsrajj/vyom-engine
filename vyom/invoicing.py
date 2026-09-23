"""invoicing -- the layer between the three payment ledgers (BusinessSubscription,
FarmPlan, BusinessApiInvoice) and (a) a unified "all payments" list for the
Billing section, and (b) a single invoice PDF/email builder that fills in
the right bill-to details depending on whether the account is individual
or business.

Kept separate from vyom/api/billing.py so the same invoice-building logic
is reachable from both the API endpoints AND the post-payment welcome-email
hook in _activate_business_subscription, without a circular import.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.email_utils import EmailSendError, render_email, send_email
from vyom.invoice_pdf import generate_invoice_pdf
from vyom.models import BusinessApiInvoice, BusinessSubscription, FarmPlan, Polygon, User

logger = logging.getLogger("vyom.invoicing")

PaymentType = Literal["business_subscription",
                      "farm_plan", "business_api_invoice"]


@dataclass
class PaymentRecord:
    """One row in the unified Billing > Payments list, regardless of which
    underlying table it came from."""
    type: PaymentType
    id: str
    invoice_number: str
    description: str
    date: datetime
    base_paise: int
    gst_paise: int
    discount_paise: int
    total_paise: int
    status: str
    payment_ref: Optional[str] = None


def _invoice_number(prefix: str, created_at: datetime, short_id: str) -> str:
    return f"{prefix}-{created_at.strftime('%Y%m')}-{short_id[:8].upper()}"


def list_payments_for_user(db: Session, user: User) -> list[PaymentRecord]:
    """Every payment (successful or not) across all three ledgers, newest
    first -- what the Billing section's "Payments" table renders. Includes
    non-paid rows (created/failed) so an abandoned checkout is still
    visible, matching how each underlying table already keeps those rows
    rather than deleting them.
    """
    out: list[PaymentRecord] = []

    subs = db.execute(
        select(BusinessSubscription).where(
            BusinessSubscription.user_id == user.id)
        .order_by(BusinessSubscription.created_at.desc())
    ).scalars().all()
    for s in subs:
        out.append(PaymentRecord(
            type="business_subscription", id=str(s.id),
            invoice_number=_invoice_number("BIZ", s.created_at, str(s.id)),
            description="Vyom Engine Business Account -- Annual Maintenance",
            date=s.created_at, base_paise=s.amount_paise, gst_paise=s.gst_paise,
            discount_paise=0, total_paise=s.total_paise, status=s.status,
            payment_ref=s.razorpay_payment_id,
        ))

    plans = db.execute(
        select(FarmPlan).where(FarmPlan.user_id == user.id)
        .order_by(FarmPlan.created_at.desc())
    ).scalars().all()
    for p in plans:
        farm = db.get(Polygon, p.farm_id)
        farm_name = farm.name if farm and farm.name else "Field"
        plan_label = {
            "individual_3m": "3-month", "individual_6m": "6-month", "individual_12m": "12-month",
        }.get(p.plan_type, p.plan_type)
        out.append(PaymentRecord(
            type="farm_plan", id=str(p.id),
            invoice_number=_invoice_number("FRM", p.created_at, str(p.id)),
            description=f"Vyom Engine Farm Plan ({plan_label}) -- {farm_name}",
            date=p.created_at, base_paise=p.base_paise, gst_paise=p.gst_paise,
            discount_paise=p.discount_paise + p.proration_credit_paise + p.wallet_applied_paise,
            total_paise=p.base_paise + p.gst_paise - p.discount_paise
            - p.proration_credit_paise - p.wallet_applied_paise,
            status=p.status, payment_ref=p.razorpay_payment_id,
        ))

    api_invoices = db.execute(
        select(BusinessApiInvoice).where(BusinessApiInvoice.user_id == user.id)
        .order_by(BusinessApiInvoice.issued_at.desc())
    ).scalars().all()
    for inv in api_invoices:
        out.append(PaymentRecord(
            type="business_api_invoice", id=str(inv.id),
            invoice_number=_invoice_number("API", inv.issued_at, str(inv.id)),
            description=f"Vyom Engine Business API -- farms created in "
            f"{inv.billing_month.strftime('%B %Y')} ({float(inv.total_area_acre):.2f} acre)",
            date=inv.issued_at, base_paise=inv.base_paise, gst_paise=inv.gst_paise,
            discount_paise=0, total_paise=inv.total_paise, status=inv.status,
            payment_ref=inv.razorpay_payment_id,
        ))

    out.sort(key=lambda r: r.date, reverse=True)
    return out


def _bill_to_for(user: User) -> dict:
    """Individual accounts are billed under the person's own name/address;
    business accounts are billed under their verified company details --
    per point 6 of the monetization spec."""
    if user.account_type == "business" and user.company_legal_name:
        return {
            "bill_to_name": user.company_legal_name,
            "bill_to_address": user.company_registered_address,
            "bill_to_gstin": user.gstin,
            "bill_to_email": user.business_email or user.email,
        }
    return {
        "bill_to_name": user.name,
        "bill_to_address": user.address,
        "bill_to_gstin": None,
        "bill_to_email": user.email,
    }


def find_payment(db: Session, user: User, payment_type: PaymentType, payment_id: str):
    """Returns the raw ORM row for a payment, scoped to this user -- callers
    must check for None (not found / not owned by this user)."""
    if payment_type == "business_subscription":
        return db.execute(select(BusinessSubscription).where(
            BusinessSubscription.id == payment_id, BusinessSubscription.user_id == user.id,
        )).scalar_one_or_none()
    if payment_type == "farm_plan":
        return db.execute(select(FarmPlan).where(
            FarmPlan.id == payment_id, FarmPlan.user_id == user.id,
        )).scalar_one_or_none()
    if payment_type == "business_api_invoice":
        return db.execute(select(BusinessApiInvoice).where(
            BusinessApiInvoice.id == payment_id, BusinessApiInvoice.user_id == user.id,
        )).scalar_one_or_none()
    return None


def build_invoice_pdf_for_record(db: Session, user: User, record: PaymentRecord) -> bytes:
    bill_to = _bill_to_for(user)
    row = find_payment(db, user, record.type, record.id)
    return generate_invoice_pdf(
        invoice_number=record.invoice_number,
        issued_at=record.date,
        line_items=[{"description": record.description,
                     "amount_paise": record.base_paise}],
        base_paise=record.base_paise, gst_paise=record.gst_paise,
        total_paise=record.total_paise, discount_paise=record.discount_paise,
        payment_ref=record.payment_ref,
        status=record.status,
        **bill_to,
    )


def email_invoice_to_user(db: Session, user: User, record: PaymentRecord,
                          extra_recipients: Optional[list[str]] = None) -> None:
    """Emails the given payment's invoice PDF to the account's own
    email(s). Raises EmailSendError on failure -- callers on a user-facing
    "email me this invoice" button should surface that; callers on a
    best-effort background hook (the welcome email) should catch and log
    instead, matching the pattern already used for notification emails.
    """
    pdf_bytes = build_invoice_pdf_for_record(db, user, record)

    recipients = {r for r in [user.email, user.business_email] if r}
    if extra_recipients:
        recipients.update(extra_recipients)
    if not recipients:
        raise EmailSendError(
            "This account has no email address on file to send the invoice to.")

    html = render_email(
        preheader=f"Your Vyom Engine invoice {record.invoice_number}",
        heading="Your invoice is ready",
        body_html=(
            f"<p>Thanks for your payment. Invoice <b>{record.invoice_number}</b> "
            f"for <b>Rs. {record.total_paise / 100:,.2f}</b> is attached as a PDF.</p>"
            f"<p>{record.description}</p>"
        ),
    )
    send_email(
        to=list(recipients),
        subject=f"Vyom Engine invoice {record.invoice_number}",
        html_body=html,
        text_fallback=f"Invoice {record.invoice_number} for Rs. {record.total_paise / 100:,.2f} is attached.",
        attachments=[(f"{record.invoice_number}.pdf",
                      pdf_bytes, "application/pdf")],
    )


def send_business_welcome_email(db: Session, user: User, sub: BusinessSubscription, *,
                                is_renewal: bool) -> None:
    """Fired right after a business subscription activates (see
    _activate_business_subscription in vyom/api/billing.py) -- to BOTH the
    account's login email and its verified company (business_email)
    address, per point 5 of the monetization spec. Best-effort: a failure
    here must never roll back or block the payment that already succeeded,
    so this is caught and logged by the caller, not raised further up.
    """
    record = PaymentRecord(
        type="business_subscription", id=str(sub.id),
        invoice_number=_invoice_number("BIZ", sub.created_at, str(sub.id)),
        description="Vyom Engine Business Account -- Annual Maintenance",
        date=sub.created_at, base_paise=sub.amount_paise, gst_paise=sub.gst_paise,
        discount_paise=0, total_paise=sub.total_paise, status=sub.status,
        payment_ref=sub.razorpay_payment_id,
    )
    pdf_bytes = build_invoice_pdf_for_record(db, user, record)

    recipients = {r for r in [user.email, user.business_email] if r}
    if not recipients:
        logger.warning(
            "Business welcome email skipped for user %s -- no email on file", user.id)
        return

    if is_renewal:
        heading = "Your Business Account has been renewed"
        intro = (f"Your Vyom Engine Business Account is renewed and active until "
                 f"{sub.ends_at.strftime('%d %b %Y') if sub.ends_at else 'your next renewal'}.")
    else:
        heading = "Welcome to Vyom Engine Business"
        intro = ("Your account has been upgraded to a Business Account. You can now "
                 "generate API credentials and create farms programmatically from "
                 "Settings &gt; Business Account.")

    html = render_email(
        preheader=heading,
        heading=heading,
        body_html=(
            f"<p>{intro}</p>"
            f"<p>Your invoice ({record.invoice_number}) is attached as a PDF.</p>"
        ),
    )
    try:
        send_email(
            to=list(recipients),
            subject=f"Vyom Engine -- {heading}",
            html_body=html,
            text_fallback=intro,
            attachments=[(f"{record.invoice_number}.pdf",
                          pdf_bytes, "application/pdf")],
        )
    except EmailSendError as exc:
        logger.error(
            "Failed to send business welcome/renewal email to %s: %s", recipients, exc)
