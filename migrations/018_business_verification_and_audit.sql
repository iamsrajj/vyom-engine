-- 018_business_verification_and_audit.sql
-- Business GST/email verification fields on users, renewal-reminder
-- de-dupe tracking, business-email OTPs, and the partner-API audit log.
-- See vyom/models.py for the SQLAlchemy models and their docstrings.

ALTER TABLE users ADD COLUMN IF NOT EXISTS gstin VARCHAR;
ALTER TABLE users ADD COLUMN IF NOT EXISTS company_legal_name VARCHAR;
ALTER TABLE users ADD COLUMN IF NOT EXISTS company_registered_address VARCHAR;
ALTER TABLE users ADD COLUMN IF NOT EXISTS gst_verified_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS business_email VARCHAR;
ALTER TABLE users ADD COLUMN IF NOT EXISTS business_email_verified_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS business_renewal_reminders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    days_before INTEGER NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_renewal_reminder_user_cycle_day UNIQUE (user_id, expires_at, days_before)
);

CREATE TABLE IF NOT EXISTS business_email_otps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email VARCHAR NOT NULL,
    otp_hash VARCHAR NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_business_email_otps_user ON business_email_otps (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS api_access_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    api_credential_id UUID REFERENCES api_credentials(id) ON DELETE SET NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    method VARCHAR NOT NULL,
    path VARCHAR NOT NULL,
    status_code INTEGER NOT NULL,
    ip_address VARCHAR,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_api_access_log_credential ON api_access_log (api_credential_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_access_log_created_at ON api_access_log (created_at);