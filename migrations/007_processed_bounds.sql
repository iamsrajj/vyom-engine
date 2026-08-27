-- Tracks the ACTUAL windowed extent that was processed for a product,
-- distinct from `footprint` (the full satellite scene footprint). Needed for
-- the reuse-check: a farm can intersect a product's footprint while falling
-- outside the narrower window that was actually processed for it.
-- Run: psql "$DATABASE_URL" -f migrations/007_processed_bounds.sql

ALTER TABLE catalog_products
    ADD COLUMN IF NOT EXISTS processed_bounds geometry(POLYGON, 4326);

CREATE INDEX IF NOT EXISTS ix_catalog_products_processed_bounds
    ON catalog_products USING GIST (processed_bounds);

-- Existing rows processed before this column existed will have
-- processed_bounds = NULL. reuse_check.py treats NULL as "no known
-- coverage" (safe/conservative -- falls through to a fresh fetch), so no
-- backfill of historical rows is required for correctness, only for
-- maximizing how much existing data becomes reusable.