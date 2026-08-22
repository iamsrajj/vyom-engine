-- Real user accounts (replaces the flat AUTH_USERS env-var list) + OTP
-- verification tracking. Run: psql "$DATABASE_URL" -f migrations/003_users_auth.sql

CREATE TABLE IF NOT EXISTS users (
    id                UUID PRIMARY KEY,
    account_id        TEXT NOT NULL UNIQUE,   -- short human-friendly id, e.g. AGD-7F3K2Q

    email             TEXT NOT NULL UNIQUE,
    google_sub        TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    profile_img_url   TEXT,

    organization      TEXT NOT NULL,
    designation       TEXT NOT NULL,
    address           TEXT NOT NULL,

    phone_cc          TEXT NOT NULL DEFAULT '91',
    phone             TEXT NOT NULL UNIQUE,
    phone_verified    BOOLEAN NOT NULL DEFAULT FALSE,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS otp_verifications (
    id                    UUID PRIMARY KEY,
    phone_cc              TEXT NOT NULL,
    phone                 TEXT NOT NULL,
    purpose               TEXT NOT NULL,   -- 'signup' | 'signin'

    otp_hash              TEXT NOT NULL,   -- sha256 of the real code, never plaintext
    provider_otp_id       TEXT,
    provider_request_id   TEXT,

    attempts              INTEGER NOT NULL DEFAULT 0,
    max_attempts          INTEGER NOT NULL DEFAULT 5,
    verified              BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at            TIMESTAMPTZ NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_otp_verifications_phone ON otp_verifications (phone);
CREATE INDEX IF NOT EXISTS ix_users_phone ON users (phone);
CREATE INDEX IF NOT EXISTS ix_users_email ON users (email);