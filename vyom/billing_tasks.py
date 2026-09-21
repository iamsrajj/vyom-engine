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
from vyom.models import (
    ApiIdempotencyKey, BusinessApiInvoice, BusinessApiInvoiceFarm, BusinessRenewalReminder,
    BusinessSubscription, FarmPlan, Polygon, User,
)
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
    <p>Your Vyom Engine API usage invoice for <strong>{invoice.billing_month.strftime('%B %Y')}</strong> is ready.</p>
    <table style="width:100%; border-collapse:collapse; margin:16px 0; font-size:14px;">
      <tr><td style="padding:4px 0; color:#5c6b60;">Farms created via API this month</td><td style="text-align:right;">{len(farms)}</td></tr>
      <tr><td style="padding:4px 0; color:#5c6b60;">Total area</td><td style="text-align:right;">{float(invoice.total_area_acre):.2f} acres</td></tr>
      <tr><td style="padding:4px 0; color:#5c6b60;">Rate</td><td style="text-align:right;">Rs.{invoice.rate_per_acre_paise / 100:.2f} / acre / year</td></tr>
      <tr><td style="padding:4px 0; color:#5c6b60;">Subtotal</td><td style="text-align:right;">Rs.{invoice.base_paise / 100:,.2f}</td></tr>
      <tr><td style="padding:4px 0; color:#5c6b60;">GST</td><td style="text-align:right;">Rs.{invoice.gst_paise / 100:,.2f}</td></tr>
      <tr style="border-top:1px solid #e6ebe2; font-weight:700;"><td style="padding:6px 0;">Total due</td><td style="text-align:right;">Rs.{invoice.total_paise / 100:,.2f}</td></tr>
    </table>
    <p>Payment is due within {settings.business_api_invoice_grace_days} days
    ({invoice.due_at.strftime('%d %b %Y')}) -- pay using the button below, or from
    the <strong>Business</strong> section of your <a href="{settings.dashboard_base_url}">Vyom Engine dashboard</a>,
    whichever is easiest. Both use the same secure Razorpay payment page.</p>
    <p>If unpaid by the due date, API access on this account is automatically suspended
    until the invoice is settled -- your dashboard and all existing farm data are not
    affected either way, and access resumes automatically the moment payment is received,
    whether that's before or after the due date.</p>
    """
    try:
        send_email(
            to=user.email,
            subject=f"Vyom Engine API invoice -- {invoice.billing_month.strftime('%B %Y')} (Rs.{invoice.total_paise / 100:,.2f} due {invoice.due_at.strftime('%d %b')})",
            html_body=render_email(
                preheader="Your monthly Vyom Engine API usage invoice is ready",
                heading="API usage invoice",
                body_html=body_html,
                cta_label="Pay now" if invoice.razorpay_payment_link_url else None,
                cta_url=invoice.razorpay_payment_link_url,
            ),
            text_fallback=f"Vyom Engine API invoice for {invoice.billing_month.strftime('%B %Y')}: "
                          f"{len(farms)} farm(s), {float(invoice.total_area_acre):.2f} acres, "
                          f"Rs.{invoice.total_paise / 100:,.2f} due by {invoice.due_at.strftime('%d %b %Y')}. "
                          f"Pay at: {invoice.razorpay_payment_link_url or '(link unavailable, contact support)'} "
                          f"or from your dashboard's Business section: {settings.dashboard_base_url}",
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


@celery_app.task(name="vyom.billing.send_business_renewal_reminders")
def send_business_renewal_reminders() -> dict:
    """Daily: emails a renewal reminder 7, 3, and 1 day before a business
    account's annual ₹999 maintenance subscription expires. De-duped via
    BusinessRenewalReminder, keyed to the SPECIFIC expires_at value so a
    renewal (which changes expires_at) naturally opens a fresh set of
    reminder slots for the next cycle."""
    db: Session = SessionLocal()
    sent = 0
    try:
        now = datetime.now(timezone.utc)
        candidates = db.execute(
            select(User).where(User.account_type == "business", User.business_status == "active",
                               User.business_expires_at.isnot(None))
        ).scalars().all()

        for user in candidates:
            days_left = (user.business_expires_at - now).days
            if days_left not in (7, 3, 1):
                continue
            already = db.execute(
                select(BusinessRenewalReminder).where(
                    BusinessRenewalReminder.user_id == user.id,
                    BusinessRenewalReminder.expires_at == user.business_expires_at,
                    BusinessRenewalReminder.days_before == days_left,
                )
            ).scalar_one_or_none()
            if already is not None:
                continue
            if not user.email:
                log_error(
                    "billing_tasks", f"Business user {user.id} has no email -- renewal reminder not sent")
                continue

            base_paise = settings.business_maintenance_fee_paise
            _, gst_paise, total_paise = total_with_gst(base_paise)
            day_word = "day" if days_left == 1 else "days"
            try:
                send_email(
                    to=user.email,
                    subject=f"Your Vyom Engine business subscription expires in {days_left} {day_word}",
                    html_body=render_email(
                        preheader=f"Renew your business subscription -- {days_left} {day_word} left",
                        heading="Time to renew your business subscription",
                        body_html=(
                            f"<p>Hi {user.name},</p>"
                            f"<p>Your Vyom Engine business account's annual maintenance subscription "
                            f"expires on <strong>{user.business_expires_at.strftime('%d %b %Y')}</strong> "
                            f"({days_left} {day_word} from now).</p>"
                            f"<p>Renewal cost: <strong>Rs.{total_paise / 100:,.2f}</strong> (incl. GST).</p>"
                            f"<p>If it lapses, your API access is suspended until renewed -- your dashboard "
                            f"and all farm data are unaffected either way. Renew any time, before or after "
                            f"expiry, from the Business section of your dashboard.</p>"
                        ),
                        cta_label="Renew now",
                        cta_url=settings.dashboard_base_url,
                    ),
                    text_fallback=f"Your Vyom Engine business subscription expires on "
                    f"{user.business_expires_at.strftime('%d %b %Y')} ({days_left} {day_word}). "
                    f"Renewal cost: Rs.{total_paise / 100:,.2f}. Renew from your dashboard: "
                    f"{settings.dashboard_base_url}",
                )
            except Exception as exc:  # noqa: BLE001
                log_error(
                    "billing_tasks", f"Failed to send renewal reminder to {user.email}: {exc}")
                continue

            db.add(BusinessRenewalReminder(
                user_id=user.id, expires_at=user.business_expires_at, days_before=days_left))
            db.commit()
            sent += 1

        logger.info(
            "send_business_renewal_reminders: sent %d reminder(s)", sent)
        return {"reminders_sent": sent}
    finally:
        db.close()


@celery_app.task(name="vyom.billing.cleanup_idempotency_keys")
def cleanup_idempotency_keys() -> dict:
    """Daily: purges partner-API idempotency-key records older than 48
    hours -- well past any realistic retry window, so replay protection is
    never lost for a genuine retry, while keeping the table from growing
    unbounded."""
    db: Session = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        result = db.execute(
            ApiIdempotencyKey.__table__.delete().where(
                ApiIdempotencyKey.created_at < cutoff)
        )
        db.commit()
        deleted = result.rowcount or 0
        logger.info("cleanup_idempotency_keys: deleted %d row(s)", deleted)
        return {"deleted": deleted}
    finally:
        db.close()


@celery_app.task(name="vyom.billing.reconcile_pending_business_subscriptions")
def reconcile_pending_business_subscriptions() -> dict:
    """Daily self-healing check: a BusinessSubscription stuck in
    status='created' for over an hour might mean the webhook delivery for
    its payment was missed or delayed (network blip, Razorpay retry
    exhaustion, etc). Polls Razorpay directly for a captured payment
    against that order and activates it if found -- this is what makes
    'auto-renew whether paid on time or late' actually robust rather than
    depending entirely on a single webhook delivery."""
    from vyom.api.billing import _activate_business_subscription

    db: Session = SessionLocal()
    healed = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        stuck = db.execute(
            select(BusinessSubscription).where(
                BusinessSubscription.status == "created", BusinessSubscription.created_at <= cutoff)
        ).scalars().all()
        for sub in stuck:
            try:
                payments = razorpay_client.get_order_payments(
                    sub.razorpay_order_id)
            except razorpay_client.RazorpayError as exc:
                log_error(
                    "billing_tasks", f"Reconcile check failed for subscription {sub.id}: {exc}")
                continue
            captured = next(
                (p for p in payments if p.get("status") == "captured"), None)
            if captured is None:
                continue
            user = db.get(User, sub.user_id)
            _activate_business_subscription(db, sub=sub, user=user, razorpay_payment_id=captured["id"],
                                            razorpay_signature=None, coupon_code=None)
            healed += 1
        logger.info(
            "reconcile_pending_business_subscriptions: healed %d", healed)
        return {"healed": healed}
    finally:
        db.close()


@celery_app.task(name="vyom.billing.reconcile_pending_business_invoices")
def reconcile_pending_business_invoices() -> dict:
    """Same self-healing idea as reconcile_pending_business_subscriptions,
    for monthly API-usage invoices (Payment Links rather than Orders)."""
    db: Session = SessionLocal()
    healed = 0
    try:
        candidates = db.execute(
            select(BusinessApiInvoice).where(
                BusinessApiInvoice.status.in_(["pending", "overdue"]),
                BusinessApiInvoice.razorpay_payment_link_id.isnot(None),
            )
        ).scalars().all()
        for invoice in candidates:
            try:
                link = razorpay_client.get_payment_link(
                    invoice.razorpay_payment_link_id)
            except razorpay_client.RazorpayError as exc:
                log_error(
                    "billing_tasks", f"Reconcile check failed for invoice {invoice.id}: {exc}")
                continue
            if link.get("status") != "paid":
                continue
            invoice.status = "paid"
            payments = link.get("payments") or []
            if payments:
                invoice.razorpay_payment_id = payments[0].get("payment_id")
            user = db.get(User, invoice.user_id)
            if user is not None:
                user.business_api_payment_status = "current"
            db.commit()
            healed += 1
        logger.info("reconcile_pending_business_invoices: healed %d", healed)
        return {"healed": healed}
    finally:
        db.close()
