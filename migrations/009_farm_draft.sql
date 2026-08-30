-- Lets a farm start as a rough placeholder (dropped pin) that immediately
-- triggers its cold-start fetch while the farmer finishes tracing the real
-- boundary, then gets finalized in place (same farm.id) once drawing
-- completes. is_draft is purely a lifecycle/display marker -- it does not
-- change fetch behavior.
-- Run: psql "$DATABASE_URL" -f migrations/009_farm_draft.sql

ALTER TABLE polygons
    ADD COLUMN IF NOT EXISTS is_draft BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS ix_polygons_is_draft ON polygons (is_draft) WHERE is_draft = TRUE;