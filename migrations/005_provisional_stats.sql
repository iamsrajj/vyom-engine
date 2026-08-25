-- Adds provisional carry-forward support to interpolated_stats:
--   - source column ('interpolated' | 'provisional')
--   - right_zonal_stat_id becomes nullable (provisional rows have no right anchor yet)
-- Run: psql "$DATABASE_URL" -f migrations/005_provisional_stats.sql
-- Safe to run even if 004_interpolated_stats.sql already created the table
-- with the old (non-nullable) schema.

ALTER TABLE interpolated_stats
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'interpolated';

ALTER TABLE interpolated_stats
    ALTER COLUMN right_zonal_stat_id DROP NOT NULL;

-- Existing rows (if any) are all real two-anchor interpolations from before
-- this feature existed -- the DEFAULT above already tags them 'interpolated'
-- correctly, no backfill needed.