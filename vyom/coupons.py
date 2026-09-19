"""coupons -- generalized coupon engine.

The 22 commercial coupon types map onto a small set of CALCULATION types
plus a shared set of ELIGIBILITY constraints that layer on top of any of
them (see Coupon's docstring in models.py). The mapping:

  Calculation types (Coupon.calculation_type):
    percentage                 -> "15% OFF"
    percentage_max_cap         -> "15% OFF up to Rs.500"
    flat                       -> "Rs.500 OFF"
    flat_min_order             -> "Rs.500 OFF on Rs.2,000+"
    buy_x_get_y                -> "Buy 2 Get 1 Free"        (get_discount_percent=100)
    buy_x_get_y_discounted     -> "Buy 2 Get 1 at 50% OFF"  (get_discount_percent=50)
    tiered                     -> "10% / 15% / 20%" by order size
    spend_x_get_y              -> "Spend Rs.5,000, get Rs.1,000 OFF"
    free_shipping               -> "Free delivery" / any named waived charge
    cashback                   -> "10% cashback"       (paid to wallet, not off the invoice)
    wallet_credit               -> "Rs.1,000 account credit" (flat top-up, unrelated to this order's amount)

  Eligibility constraints (any Coupon, regardless of calculation_type):
    Account/User-Based  -> eligible_user_ids
    First Order         -> first_order_only
    New User Coupon     -> new_user_within_days
    Plan-Specific / Subscription Discount -> eligible_plan_types
    Product-Specific    -> eligible_products
    Category-Based      -> eligible_categories
    Referral Coupon     -> requires_referral
    Recurring Coupon    -> recurring_cycles (works together with max_redemptions_per_user)
    One-Time Coupon     -> max_redemptions_per_user = 1 (the default)
    Limited-Use Coupon  -> max_redemptions (global cap)
    Partner Coupon      -> no special field needed -- it's just a code
                           distributed to one organization; eligible_user_ids
                           or a shared code with is_public=false covers it

Nothing here commits a transaction -- callers wrap redeem_coupon() together
with whatever it's paying for (a BusinessSubscription row, a future
farm_plans row) in one commit, same convention as vyom/wallet.py.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vyom.models import Coupon, CouponRedemption, User
from vyom import wallet

CALCULATION_TYPES = {
    "percentage", "percentage_max_cap", "flat", "flat_min_order",
    "buy_x_get_y", "buy_x_get_y_discounted", "tiered", "spend_x_get_y",
    "free_shipping", "cashback", "wallet_credit",
}


class CouponError(Exception):
    """Raised for any reason a coupon can't be applied -- message is safe to
    show directly to the end user (e.g. "This coupon has expired.")."""


@dataclass
class CartContext:
    order_amount_paise: int
    plan_type: str | None = None
    product: str | None = None
    category: str | None = None
    quantity: int = 1
    is_first_order: bool = False
    user_created_at: datetime | None = None
    referral_code_provided: bool = False


@dataclass
class DiscountResult:
    discount_paise: int = 0           # taken off the invoice total
    # paid to wallet AFTER the order completes (cashback/wallet_credit)
    wallet_credit_paise: int = 0
    free_qty: int = 0                 # buy_x_get_y informational quantity
    waived_charge_codes: list[str] = field(default_factory=list)


def _now():
    return datetime.now(timezone.utc)


def _check_eligibility(coupon: Coupon, user: User, cart: CartContext, redemption_count_for_user: int) -> None:
    now = _now()
    if coupon.status != "active":
        raise CouponError("This coupon is not active.")
    if coupon.starts_at and now < coupon.starts_at:
        raise CouponError("This coupon is not valid yet.")
    if coupon.expires_at and now > coupon.expires_at:
        raise CouponError("This coupon has expired.")

    if coupon.eligible_user_ids and user.id not in coupon.eligible_user_ids:
        raise CouponError("This coupon is not available on your account.")

    if coupon.first_order_only and not cart.is_first_order:
        raise CouponError("This coupon is only valid on your first order.")

    if coupon.new_user_within_days is not None:
        if cart.user_created_at is None or \
                (now - cart.user_created_at).days > coupon.new_user_within_days:
            raise CouponError("This coupon is only available to new users.")

    if coupon.eligible_plan_types and cart.plan_type not in (coupon.eligible_plan_types or []):
        raise CouponError("This coupon does not apply to this plan.")

    if coupon.eligible_products and cart.product not in (coupon.eligible_products or []):
        raise CouponError("This coupon does not apply to this product.")

    if coupon.eligible_categories and cart.category not in (coupon.eligible_categories or []):
        raise CouponError("This coupon does not apply to this category.")

    if coupon.requires_referral and not cart.referral_code_provided:
        raise CouponError("This coupon requires a valid referral.")

    if coupon.calculation_type == "flat_min_order" and coupon.min_order_paise:
        if cart.order_amount_paise < coupon.min_order_paise:
            raise CouponError(
                f"This coupon requires a minimum order of Rs.{coupon.min_order_paise / 100:.2f}.")

    if coupon.calculation_type == "spend_x_get_y" and coupon.spend_threshold_paise:
        if cart.order_amount_paise < coupon.spend_threshold_paise:
            raise CouponError(
                f"This coupon requires spending at least Rs.{coupon.spend_threshold_paise / 100:.2f}.")

    per_user_cap = coupon.recurring_cycles or coupon.max_redemptions_per_user
    if redemption_count_for_user >= per_user_cap:
        raise CouponError(
            "You have already used this coupon the maximum number of times.")


def _calculate(coupon: Coupon, cart: CartContext) -> DiscountResult:
    amt = cart.order_amount_paise
    ct = coupon.calculation_type

    if ct == "percentage":
        return DiscountResult(discount_paise=round(amt * (coupon.percent or 0) / 100))

    if ct == "percentage_max_cap":
        raw = round(amt * (coupon.percent or 0) / 100)
        capped = min(raw, coupon.max_discount_paise or raw)
        return DiscountResult(discount_paise=capped)

    if ct in ("flat", "flat_min_order"):
        return DiscountResult(discount_paise=min(coupon.flat_paise or 0, amt))

    if ct in ("buy_x_get_y", "buy_x_get_y_discounted"):
        buy_qty, get_qty = coupon.buy_qty or 1, coupon.get_qty or 0
        bundle = buy_qty + get_qty
        bundles = cart.quantity // bundle if bundle else 0
        free_qty = bundles * get_qty
        pct = 100.0 if ct == "buy_x_get_y" else (
            coupon.get_discount_percent or 0)
        discount = round(free_qty * (coupon.unit_price_paise or 0) * pct / 100)
        return DiscountResult(discount_paise=discount, free_qty=free_qty)

    if ct == "tiered":
        tiers = sorted(coupon.tiers or [], key=lambda t: t["min_paise"])
        percent = 0.0
        for tier in tiers:
            if amt >= tier["min_paise"]:
                percent = tier["percent"]
        return DiscountResult(discount_paise=round(amt * percent / 100))

    if ct == "spend_x_get_y":
        return DiscountResult(discount_paise=min(coupon.flat_paise or 0, amt))

    if ct == "free_shipping":
        return DiscountResult(waived_charge_codes=list(coupon.waived_charge_codes or []))

    if ct == "cashback":
        return DiscountResult(wallet_credit_paise=round(amt * (coupon.percent or 0) / 100))

    if ct == "wallet_credit":
        return DiscountResult(wallet_credit_paise=coupon.flat_paise or 0)

    raise CouponError(f"Unknown coupon calculation type: {ct}")


def validate_coupon(db: Session, *, code: str, user: User, cart: CartContext) -> tuple[Coupon, DiscountResult]:
    """Read-only preview: checks eligibility and returns the discount that
    WOULD be applied, without redeeming it. Use this to show the discount
    on a checkout page before the user confirms payment."""
    coupon = db.execute(select(Coupon).where(
        func.lower(Coupon.code) == code.strip().lower())).scalar_one_or_none()
    if coupon is None:
        raise CouponError("Invalid coupon code.")

    if coupon.max_redemptions is not None:
        total_redemptions = db.execute(
            select(func.count()).select_from(CouponRedemption)
            .where(CouponRedemption.coupon_id == coupon.id)
        ).scalar_one()
        if total_redemptions >= coupon.max_redemptions:
            raise CouponError("This coupon has reached its usage limit.")

    redemption_count_for_user = db.execute(
        select(func.count()).select_from(CouponRedemption)
        .where(CouponRedemption.coupon_id == coupon.id, CouponRedemption.user_id == user.id)
    ).scalar_one()

    _check_eligibility(coupon, user, cart, redemption_count_for_user)
    return coupon, _calculate(coupon, cart)


def redeem_coupon(db: Session, *, code: str, user: User, cart: CartContext,
                  order_reference_type: str, order_reference_id: UUID) -> DiscountResult:
    """Validates AND records the redemption (+ credits the wallet
    immediately for cashback/wallet_credit types). Does not commit -- call
    this from inside the same transaction as the order it discounts, and
    commit once. Re-validates from scratch (never trust a discount computed
    earlier in the request) since state (redemption counts, expiry) can
    change between a preview call and the actual charge."""
    coupon, result = validate_coupon(db, code=code, user=user, cart=cart)

    db.add(CouponRedemption(
        coupon_id=coupon.id, user_id=user.id,
        order_reference_type=order_reference_type, order_reference_id=order_reference_id,
        discount_paise=result.discount_paise, wallet_credit_paise=result.wallet_credit_paise,
    ))
    db.flush()

    if result.wallet_credit_paise:
        wallet.credit(db, user_id=user.id, amount_paise=result.wallet_credit_paise,
                      reason="coupon_cashback" if coupon.calculation_type == "cashback" else "coupon_credit",
                      reference_id=coupon.id)

    return result


def list_public_coupons(db: Session) -> list[Coupon]:
    now = _now()
    return list(db.execute(
        select(Coupon).where(
            Coupon.is_public.is_(True),
            Coupon.status == "active",
            (Coupon.starts_at.is_(None)) | (Coupon.starts_at <= now),
            (Coupon.expires_at.is_(None)) | (Coupon.expires_at >= now),
        ).order_by(Coupon.created_at.desc())
    ).scalars())
