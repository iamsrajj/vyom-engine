"""billing_tasks -- periodic Celery tasks for the farm-plan/business-invoice
billing lifecycle. Registered in celery_app.py's beat_schedule.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.celery_app import celery_app
from vyom.config import settings
from vyom.db import SessionLocal
from vyom.email_utils import render_email, send_email
from vyom.error_log import log_error
from vyom.gst import total_with_gst
from vyom.models import BusinessApiInvoice, BusinessApiInvoiceFarm, FarmPlan, Polygon, User
from vyom.units import HA_TO_ACRE
from vyom import razorpay_client, wallet

logger = logging.getLogger("vyom.billing_tasks")


@celery_app.task(name="vyom.billing.expire_farm_plans")
def expire_farm_plans() -> dict:
    """Daily: flips any active farm plan past its expires_at to 'expired'.
    Does NOT touch the farm itself -- vyom/farm_pricing.py's
    is_farm_locked() is what actually gates access, computed fresh on every
    read from expires_at vs now; this task just keeps `status` (used for
    admin/reporting visibility) in sync so it doesn't sit stale as 'active'
    forever after expiry."""
    db: Session = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expired = db.execute(
            select(FarmPlan).where(FarmPlan.status ==
                                   "active", FarmPlan.expires_at <= now)
        ).scalars().all()
        for plan in expired:
            plan.status = "expired"
        db.commit()
        logger.info(
            "expire_farm_plans: flipped %d plan(s) to expired", len(expired))
        return {"expired": len(expired)}
    finally:
        db.close()


@celery_app.task(name="vyom.billing.reconcile_abandoned_farm_plans")
def reconcile_abandoned_farm_plans() -> dict:
    """Daily: a FarmPlan can end up stuck in status='created' with a wallet
    reservation already debited (see farm_pricing._create_plan_order) if the
    user closes the Razorpay Checkout tab without completing or explicitly
    failing the payment -- there's no webhook event for "user gave up".
    After a 24h grace window (comfortably longer than any real checkout
    session), refund the wallet hold and mark the plan 'failed' so it stops
    looking like a live pending purchase."""
    db: Session = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        stuck = db.execute(
            select(FarmPlan).where(
                FarmPlan.status == "created",
                FarmPlan.wallet_applied_paise > 0,
                FarmPlan.created_at <= cutoff,
            )
        ).scalars().all()
        for plan in stuck:
            wallet.credit(db, user_id=plan.user_id, amount_paise=plan.wallet_applied_paise,
                          reason="farm_plan_refund", reference_id=plan.id)
            plan.status = "failed"
        db.commit()
        logger.info(
            "reconcile_abandoned_farm_plans: refunded %d plan(s)", len(stuck))
        return {"refunded": len(stuck)}
    finally:
        db.close()


def _previous_month_range(today: date) -> tuple[date, date]:
    """Returns (first_day_of_previous_month, first_day_of_this_month)."""
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_day_prev_month.replace(day=1)
    return first_of_prev_month, first_of_this_month


@celery_app.task(name="vyom.billing.generate_monthly_business_api_invoices")
def generate_monthly_business_api_invoices() -> dict:
    """Run on the 1st of each month (see celery_app.py's beat_schedule).
    For every business account, sums the area of farms with
    created_via='api' created during the PREVIOUS calendar month and, if
    any, generates one BusinessApiInvoice + a Razorpay Payment Link emailed
    to the account, due in settings.business_api_invoice_grace_days days.

    Ahead of the API-key platform itself (see BusinessApiInvoice's
    docstring in models.py) -- until farms can actually be created via API,
    this finds zero qualifying farms for everyone and creates nothing,
    which is the correct, safe behavior for a job that's wired up early.
    """
    db: Session = SessionLocal()
    created = 0
    try:
        period_start, period_end = _previous_month_range(date.today())
        business_users = db.execute(
            select(User).where(User.account_type == "business")
        ).scalars().all()

        for user in business_users:
            # UNIQUE (user_id, billing_month) protects against double-billing
            # if this task is ever accidentally run twice for the same month.
            already = db.execute(
                select(BusinessApiInvoice).where(
                    BusinessApiInvoice.user_id == user.id,
                    BusinessApiInvoice.billing_month == period_start,
                )
            ).scalar_one_or_none()
            if already is not None:
                continue

            farms = db.execute(
                select(Polygon).where(
                    Polygon.user_id == user.id,
                    Polygon.created_via == "api",
                    Polygon.created_at >= period_start,
                    Polygon.created_at < period_end,
                )
            ).scalars().all()
            if not farms:
                continue

            total_area_acre = sum(float(f.area_ha) * HA_TO_ACRE for f in farms)
            base_paise = round(total_area_acre *
                               settings.business_api_rate_per_acre_year_paise)
            _, gst_paise, total_paise = total_with_gst(base_paise)
            issued_at = datetime.now(timezone.utc)
            due_at = issued_at + \
                timedelta(days=settings.business_api_invoice_grace_days)

            invoice = BusinessApiInvoice(
                user_id=user.id, billing_month=period_start, total_area_acre=total_area_acre,
                rate_per_acre_paise=settings.business_api_rate_per_acre_year_paise,
                base_paise=base_paise, gst_paise=gst_paise, total_paise=total_paise,
                issued_at=issued_at, due_at=due_at, status="pending",
            )
            db.add(invoice)
            db.flush()
            for farm in farms:
                db.add(BusinessApiInvoiceFarm(
                    invoice_id=invoice.id, farm_id=farm.id,
                    area_acre_snapshot=float(farm.area_ha) * HA_TO_ACRE,
                ))

            try:
                link = razorpay_client.create_payment_link(
                    amount_paise=total_paise,
                    description=f"Vyom Engine API usage -- {period_start.strftime('%B %Y')} "
                    f"({len(farms)} farm(s), {total_area_acre:.2f} acres)",
                    customer_email=user.email or "",
                    customer_name=user.name,
                    reference_id=str(invoice.id),
                    notes={"user_id": str(
                        user.id), "billing_month": period_start.isoformat()},
                )
                invoice.razorpay_payment_link_id = link["id"]
                invoice.razorpay_payment_link_url = link["short_url"]
            except razorpay_client.RazorpayError as exc:
                log_error("billing_tasks", f"Failed to create payment link for invoice {invoice.id}: {exc}",
                          context={"user_id": str(user.id)})

            db.commit()
            created += 1
            _send_invoice_email(user, invoice, farms)

        logger.info(
            "generate_monthly_business_api_invoices: created %d invoice(s)", created)
        return {"invoices_created": created}
    finally:
        db.close()


def _send_invoice_email(user: User, invoice: BusinessApiInvoice, farms: list[Polygon]) -> None:
    if not user.email:
        log_error("billing_tasks",
                  f"Business user {user.id} has no email -- invoice {invoice.id} not sent")
        return
    body_html = f"""
    <p>Hi {user.name},</p>
    <p>Your Vyom Engine API usage invoice for <strong>{invoice.billing_month.strftime('%B %Y')}</strong>
    is ready -- {len(farms)} farm(s) created via the API this month, totalling
    {float(invoice.total_area_acre):.2f} acres.</p>
    <p><strong>Amount due: Rs.{invoice.total_paise / 100:,.2f}</strong> (incl. GST)</p>
    <p>Payment is due within {settings.business_api_invoice_grace_days} days
    ({invoice.due_at.strftime('%d %b %Y')}). If unpaid by then, API access on
    this account is automatically suspended until the invoice is settled --
    your dashboard and existing farm data are not affected either way.</p>
    """
    try:
        send_email(
            to=user.email,
            subject=f"Vyom Engine API invoice -- {invoice.billing_month.strftime('%B %Y')}",
            html_body=render_email(
                preheader="Your monthly Vyom Engine API usage invoice is ready",
                heading="API usage invoice",
                body_html=body_html,
                cta_label="Pay now" if invoice.razorpay_payment_link_url else None,
                cta_url=invoice.razorpay_payment_link_url,
            ),
            text_fallback=f"Vyom Engine API invoice for {invoice.billing_month.strftime('%B %Y')}: "
                          f"Rs.{invoice.total_paise / 100:,.2f} due by {invoice.due_at.strftime('%d %b %Y')}. "
                          f"Pay at: {invoice.razorpay_payment_link_url or '(link unavailable, contact support)'}",
        )
    except Exception as exc:  # noqa: BLE001 -- email failure must never break invoicing itself
        log_error("billing_tasks",
                  f"Failed to email invoice {invoice.id} to {user.email}: {exc}")


@celery_app.task(name="vyom.billing.suspend_overdue_business_invoices")
def suspend_overdue_business_invoices() -> dict:
    """Daily: any invoice still 'pending' past its due_at (the 15-day grace
    window already baked into due_at at generation time) gets marked
    'overdue', and the owning account's business_api_payment_status flips to
    'suspended' -- API-only, per the confirmed design decision; dashboard
    and existing farm data are untouched."""
    db: Session = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        overdue = db.execute(
            select(BusinessApiInvoice).where(
                BusinessApiInvoice.status == "pending", BusinessApiInvoice.due_at <= now)
        ).scalars().all()
        suspended_users = set()
        for invoice in overdue:
            invoice.status = "overdue"
            suspended_users.add(invoice.user_id)
        for user_id in suspended_users:
            user = db.get(User, user_id)
            if user is not None:
                user.business_api_payment_status = "suspended"
        db.commit()
        logger.info("suspend_overdue_business_invoices: %d invoice(s), %d account(s) suspended",
                    len(overdue), len(suspended_users))
        return {"overdue_invoices": len(overdue), "accounts_suspended": len(suspended_users)}
    finally:
        db.close()
