-- 016_farm_billing.sql
-- Individual per-acre farm plans (purchase/upgrade/expiry), the
-- created_via/feature_tier split on farms, and the (forward-looking, ahead
-- of the API-key platform) business monthly per-acre API invoicing tables.
-- See vyom/models.py for the SQLAlchemy models and their docstrings.

ALTER TABLE polygons ADD COLUMN IF NOT EXISTS created_via VARCHAR NOT NULL DEFAULT 'dashboard';
ALTER TABLE polygons ADD COLUMN IF NOT EXISTS feature_tier VARCHAR NOT NULL DEFAULT 'full';

ALTER TABLE users ADD COLUMN IF NOT EXISTS business_api_payment_status VARCHAR NOT NULL DEFAULT 'current';

CREATE TABLE IF NOT EXISTS farm_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    farm_id UUID NOT NULL REFERENCES polygons(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_type VARCHAR NOT NULL,
    duration_days INTEGER NOT NULL,
    area_acre_snapshot NUMERIC NOT NULL,
    rate_per_acre_paise INTEGER NOT NULL,
    base_paise INTEGER NOT NULL,
    discount_paise INTEGER NOT NULL DEFAULT 0,
    proration_credit_paise INTEGER NOT NULL DEFAULT 0,
    gst_paise INTEGER NOT NULL DEFAULT 0,
    wallet_applied_paise INTEGER NOT NULL DEFAULT 0,
    razorpay_paise INTEGER NOT NULL DEFAULT 0,
    coupon_id UUID REFERENCES coupons(id),
    upgraded_from_plan_id UUID REFERENCES farm_plans(id),
    razorpay_order_id VARCHAR UNIQUE,
    razorpay_payment_id VARCHAR UNIQUE,
    status VARCHAR NOT NULL DEFAULT 'created',
    starts_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The lookup is_farm_locked() runs on every gated read: latest plan row
-- for a farm, filtered to active ones.
CREATE INDEX IF NOT EXISTS idx_farm_plans_farm_status
    ON farm_plans (farm_id, status, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_farm_plans_user
    ON farm_plans (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS business_api_invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    billing_month DATE NOT NULL,
    total_area_acre NUMERIC NOT NULL,
    rate_per_acre_paise INTEGER NOT NULL,
    base_paise INTEGER NOT NULL,
    gst_paise INTEGER NOT NULL DEFAULT 0,
    total_paise INTEGER NOT NULL,
    razorpay_payment_link_id VARCHAR UNIQUE,
    razorpay_payment_link_url VARCHAR,
    razorpay_payment_id VARCHAR UNIQUE,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_business_invoice_user_month UNIQUE (user_id, billing_month)
);
CREATE INDEX IF NOT EXISTS idx_business_api_invoices_status_due
    ON business_api_invoices (status, due_at);

CREATE TABLE IF NOT EXISTS business_api_invoice_farms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES business_api_invoices(id) ON DELETE CASCADE,
    farm_id UUID NOT NULL REFERENCES polygons(id) ON DELETE CASCADE,
    area_acre_snapshot NUMERIC NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_business_api_invoice_farms_invoice
    ON business_api_invoice_farms (invoice_id);