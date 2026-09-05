-- Allow accounts created via phone-only signup (no Google identity yet).
-- email/google_sub were NOT NULL from the original Google-first design;
-- Postgres unique constraints already permit multiple NULLs, so relaxing
-- these to nullable doesn't weaken uniqueness for accounts that do have
-- a Google identity linked -- it only permits accounts that don't have
-- one *yet*.
ALTER TABLE users ALTER COLUMN email DROP NOT NULL;
ALTER TABLE users ALTER COLUMN google_sub DROP NOT NULL;