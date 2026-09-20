"""farm_pricing -- individual per-acre farm plan pricing (Rs.85/acre/3mo,
Rs.155/acre/6mo, Rs.295/acre/12mo), billed at farm creation and renewable
via a recharge gate once expired.

Money flow for every purchase/upgrade, in order:
  1. quote the plan (area * rate)
  2. apply a coupon discount, if any (vyom.coupons)
  3. apply a proration credit, if this is an upgrade (see upgrade_farm_plan)
  4. add GST on what's left (vyom.gst)
  5. apply wallet balance toward the total, debited immediately as a
     reservation (vyom.wallet) -- refunded back if the remaining Razorpay
     payment fails or is never completed (see reconcile_abandoned_plans in
     vyom/billing_tasks.py)
  6. whatever's left after wallet gets a Razorpay order; if wallet covered
     it completely, the plan activates immediately with no Razorpay step

Nothing here commits mid-function except where noted -- callers (the API
endpoints in vyom/api/farms.py) own the transaction boundary, same
convention as vyom/wallet.py and vyom/coupons.py.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.coupons import CartContext, CouponError, redeem_coupon
from vyom.gst import total_with_gst
from vyom.models import FarmPlan, Polygon, User
from vyom.units import HA_TO_ACRE
from vyom import razorpay_client, wallet

logger = logging.getLogger("vyom.farm_pricing")

# plan_type -> (duration_days, rate-per-acre setting)
PLAN_CONFIG = {
    "individual_3m": (90, "individual_plan_rate_3m_paise"),
    "individual_6m": (180, "individual_plan_rate_6m_paise"),
    "individual_12m": (365, "individual_plan_rate_12m_paise"),
}


class FarmPricingError(Exception):
    """Message is safe to show directly to the end user."""


def _area_acre(farm: Polygon) -> float:
    return float(farm.area_ha) * HA_TO_ACRE


@dataclass
class PlanOrderResult:
    plan_id: UUID
    # 'active' (wallet fully covered it) or 'created' (awaiting Razorpay payment)
    status: str
    base_paise: int
    discount_paise: int
    proration_credit_paise: int
    gst_paise: int
    wallet_applied_paise: int
    razorpay_paise: int              # 0 if fully covered by wallet/proration/discount
    razorpay_order_id: Optional[str] = None
    razorpay_key_id: Optional[str] = None
    expires_at: Optional[datetime] = None   # set only if status == 'active'


def _create_plan_order(db: Session, *, farm: Polygon, user: User, plan_type: str,
                       coupon_code: Optional[str], proration_credit_paise: int = 0,
                       proration_from_plan_id: Optional[UUID] = None) -> PlanOrderResult:
    if plan_type not in PLAN_CONFIG:
        raise FarmPricingError(f"Unknown plan type: {plan_type}")
    duration_days, rate_setting = PLAN_CONFIG[plan_type]
    rate_paise = getattr(settings, rate_setting)

    area_acre = _area_acre(farm)
    base_paise = round(area_acre * rate_paise)

    discount_paise = 0
    coupon_id = None
    if coupon_code:
        cart = CartContext(order_amount_paise=base_paise, plan_type=plan_type,
                           product="vyom_individual_farm", user_created_at=user.created_at)
        try:
            from vyom.coupons import validate_coupon
            coupon, result = validate_coupon(
                db, code=coupon_code, user=user, cart=cart)
            discount_paise = result.discount_paise
            coupon_id = coupon.id
        except CouponError as exc:
            raise FarmPricingError(str(exc))

    taxable_paise = max(0, base_paise - discount_paise -
                        proration_credit_paise)
    _, gst_paise, total_paise = total_with_gst(taxable_paise)

    wallet_balance = wallet.get_balance_paise(db, user.id)
    wallet_applied = min(wallet_balance, total_paise)
    razorpay_paise = total_paise - wallet_applied

    plan = FarmPlan(
        farm_id=farm.id, user_id=user.id, plan_type=plan_type, duration_days=duration_days,
        area_acre_snapshot=area_acre, rate_per_acre_paise=rate_paise, base_paise=base_paise,
        discount_paise=discount_paise, proration_credit_paise=proration_credit_paise,
        gst_paise=gst_paise, wallet_applied_paise=wallet_applied, razorpay_paise=razorpay_paise,
        coupon_id=coupon_id, upgraded_from_plan_id=proration_from_plan_id, status="created",
    )
    db.add(plan)
    db.flush()

    if wallet_applied > 0:
        # Reserved immediately, even before Razorpay payment completes --
        # see reconcile_abandoned_plans in vyom/billing_tasks.py for the
        # refund-on-abandonment safety net.
        wallet.debit(db, user_id=user.id, amount_paise=wallet_applied,
                     reason="farm_plan_reserve", reference_id=plan.id)

    if razorpay_paise == 0:
        _activate_plan(db, plan=plan, farm=farm,
                       razorpay_payment_id=None, coupon_code=coupon_code)
        db.commit()
        return PlanOrderResult(
            plan_id=plan.id, status="active", base_paise=base_paise, discount_paise=discount_paise,
            proration_credit_paise=proration_credit_paise, gst_paise=gst_paise,
            wallet_applied_paise=wallet_applied, razorpay_paise=0, expires_at=plan.expires_at,
        )

    try:
        order = razorpay_client.create_order(
            amount_paise=razorpay_paise, receipt=f"farm-plan-{plan.id}",
            notes={"farm_id": str(farm.id), "user_id": str(
                user.id), "plan_type": plan_type},
        )
    except razorpay_client.RazorpayError:
        # Undo the wallet reservation immediately rather than leaving it
        # stuck against an order that was never even created.
        if wallet_applied > 0:
            wallet.credit(db, user_id=user.id, amount_paise=wallet_applied,
                          reason="farm_plan_refund", reference_id=plan.id)
        plan.status = "failed"
        db.commit()
        raise

    plan.razorpay_order_id = order["id"]
    db.commit()

    return PlanOrderResult(
        plan_id=plan.id, status="created", base_paise=base_paise, discount_paise=discount_paise,
        proration_credit_paise=proration_credit_paise, gst_paise=gst_paise,
        wallet_applied_paise=wallet_applied, razorpay_paise=razorpay_paise,
        razorpay_order_id=order["id"], razorpay_key_id=settings.razorpay_key_id,
    )


def purchase_plan(db: Session, *, farm: Polygon, user: User, plan_type: str,
                  coupon_code: Optional[str] = None) -> PlanOrderResult:
    """New farm, or a fresh purchase for a farm with no plan at all yet."""
    return _create_plan_order(db, farm=farm, user=user, plan_type=plan_type, coupon_code=coupon_code)


def upgrade_plan(db: Session, *, farm: Polygon, user: User, new_plan_type: str,
                 coupon_code: Optional[str] = None) -> PlanOrderResult:
    """Credits the unused remaining value of the farm's current ACTIVE plan
    toward the new one -- proration is computed off the net amount actually
    funded by cash+wallet (base - discount - any earlier proration), not
    including GST, since GST is a tax on the sale, not value the farmer
    already has to carry forward. The old plan is only marked 'upgraded'
    once the new one actually activates (see _activate_plan) -- until then
    the farm stays unlocked under the OLD plan, so an abandoned upgrade
    checkout never leaves the farm in a worse state than before.
    """
    current = db.execute(
        select(FarmPlan).where(FarmPlan.farm_id ==
                               farm.id, FarmPlan.status == "active")
        .order_by(FarmPlan.created_at.desc())
    ).scalars().first()
    if current is None:
        raise FarmPricingError(
            "This farm has no active plan to upgrade from -- use purchase_plan for a fresh plan.")
    if current.plan_type == new_plan_type:
        raise FarmPricingError("This is already the farm's current plan.")

    now = datetime.now(timezone.utc)
    remaining_days = max(
        0, (current.expires_at - now).days) if current.expires_at else 0
    net_paid_paise = current.base_paise - \
        current.discount_paise - current.proration_credit_paise
    proration_credit_paise = round(net_paid_paise * remaining_days / current.duration_days) \
        if current.duration_days else 0

    return _create_plan_order(
        db, farm=farm, user=user, plan_type=new_plan_type, coupon_code=coupon_code,
        proration_credit_paise=proration_credit_paise, proration_from_plan_id=current.id,
    )


def _activate_plan(db: Session, *, plan: FarmPlan, farm: Polygon, razorpay_payment_id: Optional[str],
                   coupon_code: Optional[str]) -> None:
    """Idempotent on plan.status -- safe to call from both the fast
    frontend-verify path and the authoritative webhook, whichever arrives
    first, same pattern as _activate_business_subscription in
    vyom/api/billing.py. Reads plan.upgraded_from_plan_id (persisted at
    order-creation time) rather than taking it as a parameter, so it
    survives the async round trip to the webhook correctly."""
    if plan.status == "active":
        return

    now = datetime.now(timezone.utc)
    plan.razorpay_payment_id = razorpay_payment_id
    plan.status = "active"
    plan.starts_at = now
    plan.expires_at = now + timedelta(days=plan.duration_days)

    if plan.upgraded_from_plan_id is not None:
        old_plan = db.get(FarmPlan, plan.upgraded_from_plan_id)
        if old_plan is not None and old_plan.status == "active":
            old_plan.status = "upgraded"

    if coupon_code and plan.coupon_id is not None:
        user = db.get(User, plan.user_id)
        cart = CartContext(order_amount_paise=plan.base_paise, plan_type=plan.plan_type,
                           product="vyom_individual_farm", user_created_at=user.created_at)
        try:
            redeem_coupon(db, code=coupon_code, user=user, cart=cart,
                          order_reference_type="farm_plan", order_reference_id=plan.id)
        except CouponError as exc:
            # Same reasoning as the business-subscription path: payment
            # already succeeded, a coupon that stopped being valid between
            # order-creation and payment must never block activation.
            from vyom.error_log import log_error
            log_error("farm_pricing", f"Coupon redemption failed post-payment: {exc}",
                      context={"plan_id": str(plan.id), "coupon_code": coupon_code})

    db.flush()


def activate_plan_from_webhook(db: Session, *, plan: FarmPlan, farm: Polygon, razorpay_payment_id: str) -> None:
    """Public entry point for vyom/api/billing.py's webhook handler --
    thin wrapper around _activate_plan so the webhook doesn't need to reach
    into this module's private function directly. No coupon_code here: a
    webhook has no way to know what coupon (if any) the checkout used --
    coupon redemption for the farm-plan path happens on the frontend-verify
    call (verify_and_activate below), which does have it. If a webhook is
    the ONLY confirmation that ever arrives (verify never got called), a
    coupon on that purchase silently never gets redeemed -- flagged as a
    known gap, same shape as the business-subscription path's coupon
    handling in vyom/api/billing.py."""
    _activate_plan(db, plan=plan, farm=farm,
                   razorpay_payment_id=razorpay_payment_id, coupon_code=None)


def is_farm_locked(db: Session, farm: Polygon) -> tuple[bool, Optional[FarmPlan]]:
    """The single source of truth for the 'recharge to continue' gate --
    every gated endpoint in vyom/api/farms.py calls this rather than
    re-deriving lock logic inline.

    Farms with created_via='api' are billed through BusinessApiInvoice
    (monthly, in arrears -- see vyom/billing_tasks.py), never through
    FarmPlan, so they never have a FarmPlan row at all and must NEVER be
    treated as locked here -- enforcement for those lives entirely at the
    account level (User.business_api_payment_status), checked by
    vyom/api_auth.py's partner-API auth gate, not per-farm. Getting this
    backwards would have every API-created farm shown in the dashboard
    incorrectly appear locked, since it has no active FarmPlan by design.

    A dashboard-created farm (created_via='dashboard', the default) with NO
    plan row at all (shouldn't normally happen once purchase-at-creation is
    enforced, but handled defensively) counts as locked.
    """
    if farm.created_via == "api":
        return False, None

    plan = db.execute(
        select(FarmPlan).where(FarmPlan.farm_id ==
                               farm.id, FarmPlan.status == "active")
        .order_by(FarmPlan.created_at.desc())
    ).scalars().first()
    if plan is None:
        return True, None
    now = datetime.now(timezone.utc)
    if plan.expires_at and plan.expires_at <= now:
        return True, plan
    return False, plan


def verify_and_activate(db: Session, *, plan: FarmPlan, farm: Polygon,
                        razorpay_payment_id: str, razorpay_signature: str,
                        coupon_code: Optional[str]) -> None:
    if not razorpay_client.verify_payment_signature(
        order_id=plan.razorpay_order_id, payment_id=razorpay_payment_id, signature=razorpay_signature,
    ):
        raise FarmPricingError("Payment could not be verified.")
    _activate_plan(db, plan=plan, farm=farm, razorpay_payment_id=razorpay_payment_id,
                   coupon_code=coupon_code)
    db.commit()
