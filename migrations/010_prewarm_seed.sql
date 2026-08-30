-- Marks purely synthetic seed polygons created by the prewarm tool, distinct
-- from is_draft (a real farm mid-creation). See models.py's Polygon docstring
-- for is_prewarm_seed.
-- Run: psql "$DATABASE_URL" -f migrations/010_prewarm_seed.sql

ALTER TABLE polygons
    ADD COLUMN IF NOT EXISTS is_prewarm_seed BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS ix_polygons_is_prewarm_seed ON polygons (is_prewarm_seed) WHERE is_prewarm_seed = TRUE;