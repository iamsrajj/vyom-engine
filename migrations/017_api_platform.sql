-- 017_api_platform.sql
-- Business-account API credentials and request idempotency, backing
-- points 2/3 of the monetization spec. See vyom/models.py for the
-- SQLAlchemy models and their docstrings.

CREATE TABLE IF NOT EXISTS api_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    api_key VARCHAR UNIQUE NOT NULL,
    api_secret_hash VARCHAR NOT NULL,
    secret_last4 VARCHAR NOT NULL,
    label VARCHAR,
    status VARCHAR NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revoked_reason VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_api_credentials_user ON api_credentials (user_id);

CREATE TABLE IF NOT EXISTS api_idempotency_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    api_credential_id UUID NOT NULL REFERENCES api_credentials(id) ON DELETE CASCADE,
    idempotency_key VARCHAR NOT NULL,
    request_hash VARCHAR NOT NULL,
    response_status INTEGER NOT NULL,
    response_body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_idempotency_credential_key UNIQUE (api_credential_id, idempotency_key)
);
-- Lets a periodic cleanup job (not yet built -- these rows are small and
-- low-volume enough to defer) purge entries older than, say, 48h.
CREATE INDEX IF NOT EXISTS idx_api_idempotency_created_at ON api_idempotency_keys (created_at);