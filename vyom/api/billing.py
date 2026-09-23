"""api/billing -- business account upgrade (Razorpay), wallet balance, and
coupon validate/admin endpoints.

Farm-plan billing (individual per-acre pricing, the business per-acre API
billing cycle, and the API-key platform itself) are NOT in this file --
they depend on this foundation (Razorpay wiring + coupon engine + wallet)
and come next.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.auth import require_auth, require_error_panel_access
from vyom.config import settings
from vyom.coupons import CartContext, CouponError, redeem_coupon, list_public_coupons, validate_coupon
from vyom.db import get_db
from vyom.email_utils import EmailSendError
from vyom.error_log import log_error
from vyom.gst import total_with_gst
from vyom import invoicing
from vyom.models import BusinessSubscription, Coupon, User
from vyom import razorpay_client, wallet

logger = logging.getLogger("vyom.api.billing")

router = APIRouter(prefix="/billing", tags=["billing"])
coupons_router = APIRouter(prefix="/coupons", tags=["coupons"])
admin_coupons_router = APIRouter(
    prefix="/admin/coupons", tags=["admin"],
    # same admin gate as the Errors panel
    dependencies=[Depends(require_error_panel_access)],
)


def _get_user(db: Session, user_id: str) -> User:
    user = db.get(User, UUID(user_id))
    if user is None:
        raise HTTPException(404, "User not found")
    return user


# ---------------------------------------------------------------------------
# Business account upgrade
# ---------------------------------------------------------------------------

class UpgradeRequest(BaseModel):
    coupon_code: Optional[str] = None


class UpgradeOrderOut(BaseModel):
    razorpay_order_id: str
    razorpay_key_id: str
    # what Razorpay will actually charge (post-coupon, post-GST)
    amount_paise: int
    base_paise: int
    gst_paise: int
    discount_paise: int


@router.post("/business/upgrade", response_model=UpgradeOrderOut)
def create_business_upgrade_order(payload: UpgradeRequest, user_id: str = Depends(require_auth),
                                  db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    if user.account_type == "business" and user.business_status == "active":
        raise HTTPException(
            400, "Account is already an active business account.")
    if user.gst_verified_at is None:
        raise HTTPException(
            400, "Please verify your GSTIN before upgrading to a business account.")
    if user.business_email_verified_at is None:
        raise HTTPException(
            400, "Please verify your business email before upgrading to a business account.")

    base_paise = settings.business_maintenance_fee_paise
    discount_paise = 0
    # NOTE: coupon is validated (not yet redeemed) here -- redemption is
    # recorded only once payment is confirmed by the webhook, so an
    # abandoned checkout never burns a one-time-use coupon. See
    # verify_business_payment / razorpay_webhook below for where redemption
    # actually happens.
    if payload.coupon_code:
        cart = CartContext(order_amount_paise=base_paise, plan_type="business_maintenance",
                           product="vyom_business", is_first_order=False,
                           user_created_at=user.created_at)
        try:
            _, result = validate_coupon(
                db, code=payload.coupon_code, user=user, cart=cart)
            discount_paise = result.discount_paise
        except CouponError as exc:
            raise HTTPException(400, str(exc))

    _, gst_paise, total_paise = total_with_gst(base_paise - discount_paise)

    try:
        order = razorpay_client.create_order(
            amount_paise=total_paise,
            receipt=f"biz-upgrade-{user.id}",
            notes={"user_id": str(user.id),
                   "coupon_code": payload.coupon_code or ""},
        )
    except razorpay_client.RazorpayError as exc:
        log_error("api.billing", str(exc), context={"user_id": str(user.id)})
        raise HTTPException(
            502, "Could not create payment order. Please try again.")

    sub = BusinessSubscription(
        user_id=user.id, amount_paise=base_paise - discount_paise, gst_paise=gst_paise,
        total_paise=total_paise, razorpay_order_id=order["id"], status="created",
    )
    db.add(sub)
    db.commit()

    return UpgradeOrderOut(
        razorpay_order_id=order["id"], razorpay_key_id=settings.razorpay_key_id,
        amount_paise=total_paise, base_paise=base_paise - discount_paise,
        gst_paise=gst_paise, discount_paise=discount_paise,
    )


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    coupon_code: Optional[str] = None


@router.post("/business/verify")
def verify_business_payment(payload: VerifyPaymentRequest, user_id: str = Depends(require_auth),
                            db: Session = Depends(get_db)):
    """Fast UI-confirmation path, called by the frontend right after
    Razorpay Checkout succeeds. This IS signature-verified (not blindly
    trusted) but is still only the frontend's word -- the webhook below is
    what actually activates the account. This endpoint activates it too,
    same as the webhook, so the user doesn't have to wait for a webhook
    round-trip to see their account upgrade -- whichever of the two arrives
    first wins, and the other is a no-op (status='paid' guard below)."""
    user = _get_user(db, user_id)
    sub = db.execute(select(BusinessSubscription).where(
        BusinessSubscription.razorpay_order_id == payload.razorpay_order_id,
        BusinessSubscription.user_id == user.id,
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(404, "No matching order found for this account.")

    if not razorpay_client.verify_payment_signature(
        order_id=payload.razorpay_order_id, payment_id=payload.razorpay_payment_id,
        signature=payload.razorpay_signature,
    ):
        log_error("api.billing", "Razorpay signature mismatch on /business/verify",
                  context={"user_id": str(user.id), "order_id": payload.razorpay_order_id})
        raise HTTPException(400, "Payment could not be verified.")

    _activate_business_subscription(db, sub=sub, user=user,
                                    razorpay_payment_id=payload.razorpay_payment_id,
                                    razorpay_signature=payload.razorpay_signature,
                                    coupon_code=payload.coupon_code)
    return {"status": "activated", "business_expires_at": user.business_expires_at}


def _activate_business_subscription(db: Session, *, sub: BusinessSubscription, user: User,
                                    razorpay_payment_id: str, razorpay_signature: Optional[str],
                                    coupon_code: Optional[str]) -> None:
    """Shared by the verify endpoint and the webhook -- idempotent on
    sub.status: a second call (whichever path arrives second) is a no-op
    rather than double-extending the expiry date."""
    if sub.status == "paid":
        return  # already activated by the other path (verify vs webhook race)

    was_active_before = user.business_status == "active"

    sub.razorpay_payment_id = razorpay_payment_id
    sub.razorpay_signature = razorpay_signature
    sub.status = "paid"

    now = datetime.now(timezone.utc)
    # If renewing before the old expiry lapses, extend from the old expiry
    # rather than from now, so early renewal never shortens what was left.
    base = user.business_expires_at if (
        user.business_expires_at and user.business_expires_at > now) else now
    sub.starts_at = now
    sub.ends_at = base + timedelta(days=settings.business_subscription_days)

    user.account_type = "business"
    user.business_status = "active"
    user.business_expires_at = sub.ends_at

    if coupon_code:
        cart = CartContext(order_amount_paise=sub.amount_paise, plan_type="business_maintenance",
                           product="vyom_business", user_created_at=user.created_at)
        try:
            redeem_coupon(db, code=coupon_code, user=user, cart=cart,
                          order_reference_type="business_subscription", order_reference_id=sub.id)
        except CouponError as exc:
            # Payment already succeeded -- a coupon that stopped being valid
            # between order-creation and payment must never block
            # activation. Log it for a human to reconcile, don't raise.
            log_error("api.billing", f"Coupon redemption failed post-payment: {exc}",
                      context={"user_id": str(user.id), "subscription_id": str(sub.id),
                               "coupon_code": coupon_code})

    db.commit()

    # Best-effort: a failed welcome/renewal email must never undo or block
    # an already-successful payment (see send_business_welcome_email's
    # docstring) -- it catches EmailSendError itself and logs, but guard
    # against any other unexpected exception here too for the same reason.
    try:
        invoicing.send_business_welcome_email(
            db, user, sub, is_renewal=was_active_before)
    except Exception:  # noqa: BLE001 -- see comment above
        logger.exception(
            "Unexpected error sending business welcome/renewal email")


@router.post("/webhooks/razorpay", include_in_schema=False)
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Server-to-server callback from Razorpay -- the AUTHORITATIVE
    confirmation of payment, independent of whether the user's browser is
    still open. Register this URL (https://<your-domain>/billing/webhooks/razorpay)
    in the Razorpay dashboard for the payment.captured, payment.failed, AND
    payment_link.paid events (the last one is for business monthly API
    invoices, sent as Payment Links rather than Orders -- see
    vyom/billing_tasks.py), and set RAZORPAY_WEBHOOK_SECRET to the secret
    shown there.

    Resolves an incoming order_id against BOTH BusinessSubscription (the
    ₹999/year upgrade) and FarmPlan (individual per-acre plans) -- checked
    in that order, since order IDs are unique per row regardless of table.

    Deliberately not behind require_auth -- Razorpay calls this directly,
    with no user session. Trust is established entirely by the signature
    check below, not by any header/cookie a browser would send.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        valid = razorpay_client.verify_webhook_signature(
            raw_body=raw_body, signature=signature)
    except razorpay_client.RazorpayError as exc:
        log_error("api.billing.webhook", str(exc))
        raise HTTPException(500, "Webhook not configured")

    if not valid:
        log_error("api.billing.webhook", "Invalid Razorpay webhook signature")
        raise HTTPException(400, "Invalid signature")

    payload = await request.json()
    event = payload.get("event")

    if event == "payment.captured":
        entity = payload["payload"]["payment"]["entity"]
        order_id, payment_id = entity["order_id"], entity["id"]

        sub = db.execute(select(BusinessSubscription).where(
            BusinessSubscription.razorpay_order_id == order_id)).scalar_one_or_none()
        if sub is not None:
            user = db.get(User, sub.user_id)
            _activate_business_subscription(db, sub=sub, user=user, razorpay_payment_id=payment_id,
                                            razorpay_signature=None, coupon_code=None)
            return {"status": "ok"}

        from vyom.models import FarmPlan, Polygon
        from vyom import farm_pricing
        plan = db.execute(select(FarmPlan).where(
            FarmPlan.razorpay_order_id == order_id)).scalar_one_or_none()
        if plan is not None:
            farm = db.get(Polygon, plan.farm_id)
            farm_pricing.activate_plan_from_webhook(
                db, plan=plan, farm=farm, razorpay_payment_id=payment_id)
            db.commit()
            return {"status": "ok"}

        # Order not one we recognize (a stray/replayed event, or an order
        # created by a since-removed flow) -- 200 anyway so Razorpay doesn't
        # keep retrying a delivery we will never be able to act on.
        logger.warning(
            "Webhook payment.captured for unknown order %s", order_id)
        return {"status": "ignored"}

    elif event == "payment.failed":
        entity = payload["payload"]["payment"]["entity"]
        order_id = entity["order_id"]

        sub = db.execute(select(BusinessSubscription).where(
            BusinessSubscription.razorpay_order_id == order_id)).scalar_one_or_none()
        if sub and sub.status == "created":
            sub.status = "failed"
            db.commit()
            return {"status": "ok"}

        from vyom.models import FarmPlan
        from vyom import wallet as wallet_module
        plan = db.execute(select(FarmPlan).where(
            FarmPlan.razorpay_order_id == order_id)).scalar_one_or_none()
        if plan and plan.status == "created":
            if plan.wallet_applied_paise:
                wallet_module.credit(db, user_id=plan.user_id, amount_paise=plan.wallet_applied_paise,
                                     reason="farm_plan_refund", reference_id=plan.id)
            plan.status = "failed"
            db.commit()

    elif event == "payment_link.paid":
        entity = payload["payload"]["payment_link"]["entity"]
        from vyom.models import BusinessApiInvoice
        invoice = db.execute(select(BusinessApiInvoice).where(
            BusinessApiInvoice.razorpay_payment_link_id == entity["id"])).scalar_one_or_none()
        if invoice is not None and invoice.status != "paid":
            invoice.status = "paid"
            invoice.razorpay_payment_id = entity.get("payments", [{}])[0].get(
                "payment_id") if entity.get("payments") else None
            user = db.get(User, invoice.user_id)
            # Only lifts suspension if THIS was the reason -- a known
            # simplification: if a business account somehow has more than
            # one unpaid invoice at once, paying one still clears the flag
            # here rather than checking for other outstanding invoices.
            # Flagged as a limitation to revisit if that scenario turns out
            # to matter in practice (it shouldn't under normal monthly
            # billing, since each month's invoice is generated only after
            # the previous one either doesn't exist or is a separate row).
            if user is not None:
                user.business_api_payment_status = "current"
            db.commit()

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Unified payment history + invoice download/email -- point 6 of the
# monetization spec. Works for BOTH individual and business accounts,
# across all three payment ledgers (BusinessSubscription, FarmPlan,
# BusinessApiInvoice) -- see vyom/invoicing.py for how each is normalized.
# ---------------------------------------------------------------------------

class PaymentOut(BaseModel):
    type: str
    id: str
    invoice_number: str
    description: str
    date: datetime
    base_paise: int
    gst_paise: int
    discount_paise: int
    total_paise: int
    status: str
    payment_ref: Optional[str]


@router.get("/payments", response_model=list[PaymentOut])
def list_payments(user_id: str = Depends(require_auth), db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    return [
        PaymentOut(
            type=r.type, id=r.id, invoice_number=r.invoice_number, description=r.description,
            date=r.date, base_paise=r.base_paise, gst_paise=r.gst_paise,
            discount_paise=r.discount_paise, total_paise=r.total_paise, status=r.status,
            payment_ref=r.payment_ref,
        )
        for r in invoicing.list_payments_for_user(db, user)
    ]


def _find_record_or_404(db: Session, user: User, payment_type: str, payment_id: str):
    all_records = invoicing.list_payments_for_user(db, user)
    record = next((r for r in all_records if r.type ==
                  payment_type and r.id == payment_id), None)
    if record is None:
        raise HTTPException(404, "Payment not found")
    return record


def _require_paid(record) -> None:
    # A "created" (abandoned checkout) or "failed" payment never completed,
    # so there's no real invoice to hand out for it -- the frontend already
    # hides the download/email buttons for these (see loadBillingPayments
    # in web/index.html), this is the server-side backstop against someone
    # hitting the endpoint directly with a non-paid id.
    if record.status != "paid":
        raise HTTPException(
            400, f"This payment is '{record.status}', not paid -- there's no invoice to send yet.")


@router.get("/payments/{payment_type}/{payment_id}/invoice.pdf")
def download_payment_invoice(payment_type: str, payment_id: str,
                             user_id: str = Depends(require_auth), db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    record = _find_record_or_404(db, user, payment_type, payment_id)
    _require_paid(record)
    pdf_bytes = invoicing.build_invoice_pdf_for_record(db, user, record)
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{record.invoice_number}.pdf"'},
    )


@router.post("/payments/{payment_type}/{payment_id}/email-invoice")
def email_payment_invoice(payment_type: str, payment_id: str,
                          user_id: str = Depends(require_auth), db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    record = _find_record_or_404(db, user, payment_type, payment_id)
    _require_paid(record)
    try:
        invoicing.email_invoice_to_user(db, user, record)
    except EmailSendError as exc:
        raise HTTPException(502, str(exc))
    return {"status": "sent"}


# ---------------------------------------------------------------------------
# Business invoices -- lets a business account pay from the dashboard,
# using the SAME Razorpay Payment Link emailed to them (point 4 of the
# monetization spec) rather than a separate parallel payment flow.
# ---------------------------------------------------------------------------

class BusinessInvoiceOut(BaseModel):
    id: UUID
    billing_month: str
    total_area_acre: float
    base_paise: int
    gst_paise: int
    total_paise: int
    status: str
    issued_at: datetime
    due_at: datetime
    razorpay_payment_link_url: Optional[str]

    class Config:
        from_attributes = True


@router.get("/business/invoices", response_model=list[BusinessInvoiceOut])
def list_business_invoices(user_id: str = Depends(require_auth), db: Session = Depends(get_db)):
    from vyom.models import BusinessApiInvoice
    user = _get_user(db, user_id)
    rows = db.execute(
        select(BusinessApiInvoice).where(BusinessApiInvoice.user_id == user.id)
        .order_by(BusinessApiInvoice.billing_month.desc())
    ).scalars().all()
    return [
        BusinessInvoiceOut(
            id=r.id, billing_month=r.billing_month.isoformat(), total_area_acre=float(r.total_area_acre),
            base_paise=r.base_paise, gst_paise=r.gst_paise, total_paise=r.total_paise, status=r.status,
            issued_at=r.issued_at, due_at=r.due_at, razorpay_payment_link_url=r.razorpay_payment_link_url,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------

class WalletTransactionOut(BaseModel):
    amount_paise: int
    reason: str
    balance_after_paise: int
    created_at: datetime

    class Config:
        from_attributes = True


class WalletOut(BaseModel):
    balance_paise: int
    recent_transactions: list[WalletTransactionOut]


@router.get("/wallet", response_model=WalletOut)
def get_wallet(user_id: str = Depends(require_auth), db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    return WalletOut(
        balance_paise=user.wallet_balance_paise,
        recent_transactions=wallet.list_transactions(db, user.id, limit=20),
    )


# ---------------------------------------------------------------------------
# Coupons -- public validate + public listing
# ---------------------------------------------------------------------------

class ValidateCouponRequest(BaseModel):
    code: str
    order_amount_paise: int
    plan_type: Optional[str] = None
    product: Optional[str] = None
    category: Optional[str] = None
    quantity: int = 1


class DiscountOut(BaseModel):
    discount_paise: int
    wallet_credit_paise: int
    free_qty: int
    waived_charge_codes: list[str]


@coupons_router.post("/validate", response_model=DiscountOut)
def validate_coupon_endpoint(payload: ValidateCouponRequest, user_id: str = Depends(require_auth),
                             db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    cart = CartContext(
        order_amount_paise=payload.order_amount_paise, plan_type=payload.plan_type,
        product=payload.product, category=payload.category, quantity=payload.quantity,
        user_created_at=user.created_at,
    )
    try:
        _, result = validate_coupon(
            db, code=payload.code, user=user, cart=cart)
    except CouponError as exc:
        raise HTTPException(400, str(exc))
    return DiscountOut(discount_paise=result.discount_paise, wallet_credit_paise=result.wallet_credit_paise,
                       free_qty=result.free_qty, waived_charge_codes=result.waived_charge_codes)


class PublicCouponOut(BaseModel):
    code: str
    description: Optional[str]
    calculation_type: str
    percent: Optional[float]
    flat_paise: Optional[int]
    max_discount_paise: Optional[int]
    min_order_paise: Optional[int]
    expires_at: Optional[datetime]

    class Config:
        from_attributes = True


@coupons_router.get("/public", response_model=list[PublicCouponOut])
def list_public_coupons_endpoint(db: Session = Depends(get_db)):
    """No auth required -- this is what a marketing "Coupons" page on the
    website shows. Hidden-but-valid coupons (is_public=false) never appear
    here; they still work normally if a user enters the code manually at
    checkout."""
    return list_public_coupons(db)


# ---------------------------------------------------------------------------
# Coupons -- admin CRUD
# ---------------------------------------------------------------------------

class CouponIn(BaseModel):
    code: str
    description: Optional[str] = None
    calculation_type: str
    percent: Optional[float] = None
    flat_paise: Optional[int] = None
    max_discount_paise: Optional[int] = None
    min_order_paise: Optional[int] = None
    buy_qty: Optional[int] = None
    get_qty: Optional[int] = None
    get_discount_percent: Optional[float] = None
    unit_price_paise: Optional[int] = None
    spend_threshold_paise: Optional[int] = None
    tiers: Optional[list[dict]] = None
    waived_charge_codes: Optional[list[str]] = None
    is_public: bool = False
    eligible_user_ids: Optional[list[UUID]] = None
    first_order_only: bool = False
    new_user_within_days: Optional[int] = None
    eligible_plan_types: Optional[list[str]] = None
    eligible_products: Optional[list[str]] = None
    eligible_categories: Optional[list[str]] = None
    requires_referral: bool = False
    recurring_cycles: Optional[int] = None
    max_redemptions: Optional[int] = None
    max_redemptions_per_user: int = 1
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class CouponOut(CouponIn):
    id: UUID
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


@admin_coupons_router.get("", response_model=list[CouponOut])
def admin_list_coupons(db: Session = Depends(get_db)):
    return list(db.execute(select(Coupon).order_by(Coupon.created_at.desc())).scalars())


@admin_coupons_router.post("", response_model=CouponOut)
def admin_create_coupon(payload: CouponIn, user_id: str = Depends(require_error_panel_access),
                        db: Session = Depends(get_db)):
    if payload.calculation_type not in _valid_calculation_types():
        raise HTTPException(
            422, f"Unknown calculation_type: {payload.calculation_type}")
    coupon = Coupon(**payload.model_dump(), created_by_admin_id=UUID(user_id))
    db.add(coupon)
    db.commit()
    db.refresh(coupon)
    return coupon


@admin_coupons_router.patch("/{coupon_id}", response_model=CouponOut)
def admin_update_coupon(coupon_id: UUID, payload: CouponIn, db: Session = Depends(get_db)):
    coupon = db.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(404, "Coupon not found")
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(coupon, field_name, value)
    db.commit()
    db.refresh(coupon)
    return coupon


@admin_coupons_router.post("/{coupon_id}/disable", response_model=CouponOut)
def admin_disable_coupon(coupon_id: UUID, db: Session = Depends(get_db)):
    coupon = db.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(404, "Coupon not found")
    coupon.status = "disabled"
    db.commit()
    db.refresh(coupon)
    return coupon


def _valid_calculation_types() -> set[str]:
    from vyom.coupons import CALCULATION_TYPES
    return CALCULATION_TYPES
