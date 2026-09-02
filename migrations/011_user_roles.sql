-- Real role-based access control, replacing the broken ERROR_PANEL_USERNAMES
-- allowlist (fail-open when unset, and permanently unsatisfiable for real
-- Google/OTP accounts since it compared UUIDs against plain usernames).
-- Run: psql "$DATABASE_URL" -f migrations/011_user_roles.sql
--
-- After running this, promote your first real admin manually:
--   UPDATE users SET role = 'admin' WHERE email = '<your email>';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user';