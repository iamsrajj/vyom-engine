-- 015_billing_foundation.sql
-- Business accounts (Razorpay-billed maintenance fee), a real wallet
-- ledger, and a generalized coupon engine. See vyom/models.py for the
-- SQLAlchemy models these back and their docstrings for design rationale.

ALTER TABLE users ADD COLUMN IF NOT EXISTS account_type VARCHAR NOT NULL DEFAULT 'individual';
ALTER TABLE users ADD COLUMN IF NOT EXISTS business_status VARCHAR;
ALTER TABLE users ADD COLUMN IF NOT EXISTS business_expires_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_balance_paise INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS business_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_paise INTEGER NOT NULL,
    gst_paise INTEGER NOT NULL DEFAULT 0,
    total_paise INTEGER NOT NULL,
    razorpay_order_id VARCHAR UNIQUE NOT NULL,
    razorpay_payment_id VARCHAR UNIQUE,
    razorpay_signature VARCHAR,
    status VARCHAR NOT NULL DEFAULT 'created',
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_business_subscriptions_user
    ON business_subscriptions (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_paise INTEGER NOT NULL,
    reason VARCHAR NOT NULL,
    reference_id UUID,
    balance_after_paise INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wallet_transactions_user
    ON wallet_transactions (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS coupons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR UNIQUE NOT NULL,
    description TEXT,
    calculation_type VARCHAR NOT NULL,

    percent DOUBLE PRECISION,
    flat_paise INTEGER,
    max_discount_paise INTEGER,
    min_order_paise INTEGER,
    buy_qty INTEGER,
    get_qty INTEGER,
    get_discount_percent DOUBLE PRECISION,
    unit_price_paise INTEGER,
    spend_threshold_paise INTEGER,
    tiers JSONB,
    waived_charge_codes VARCHAR[],

    is_public BOOLEAN NOT NULL DEFAULT false,
    eligible_user_ids UUID[],
    first_order_only BOOLEAN NOT NULL DEFAULT false,
    new_user_within_days INTEGER,
    eligible_plan_types VARCHAR[],
    eligible_products VARCHAR[],
    eligible_categories VARCHAR[],
    requires_referral BOOLEAN NOT NULL DEFAULT false,
    recurring_cycles INTEGER,

    max_redemptions INTEGER,
    max_redemptions_per_user INTEGER NOT NULL DEFAULT 1,

    starts_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    status VARCHAR NOT NULL DEFAULT 'active',

    created_by_admin_id UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- is_public + status + expiry is exactly the filter the public coupon-
-- listing endpoint runs on every load.
CREATE INDEX IF NOT EXISTS idx_coupons_public_active
    ON coupons (is_public, status, expires_at);

CREATE TABLE IF NOT EXISTS coupon_redemptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    coupon_id UUID NOT NULL REFERENCES coupons(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    order_reference_type VARCHAR NOT NULL,
    order_reference_id UUID NOT NULL,
    discount_paise INTEGER NOT NULL DEFAULT 0,
    wallet_credit_paise INTEGER NOT NULL DEFAULT 0,
    redeemed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Used to enforce max_redemptions_per_user (COUNT per coupon+user) and
-- max_redemptions (COUNT per coupon) on every validation call.
CREATE INDEX IF NOT EXISTS idx_coupon_redemptions_coupon_user
    ON coupon_redemptions (coupon_id, user_id);