-- Lets a genuinely cold-start farm's first product get a wider processing
-- window (~3.3km default, see settings.cold_start_buffer_deg) instead of
-- the normal ~500m buffer, so neighboring farms added afterward fall
-- inside it and hit the reuse-check instant path instead of each
-- triggering their own CDSE fetch.
-- Run: psql "$DATABASE_URL" -f migrations/008_cold_start_buffer.sql

ALTER TABLE catalog_products
    ADD COLUMN IF NOT EXISTS cold_start_buffer_deg NUMERIC;