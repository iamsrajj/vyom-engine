-- Pixel-level (raster) counterpart to interpolated_stats. Provisional rows
-- reuse a real product's existing COG (no new file); interpolated rows point
-- at a genuinely new pixel-wise-interpolated COG written by raster_interpolation.py.
-- Run: psql "$DATABASE_URL" -f migrations/006_interpolated_tiles.sql

CREATE TABLE IF NOT EXISTS interpolated_tiles (
    id                BIGSERIAL PRIMARY KEY,
    polygon_id        UUID NOT NULL REFERENCES polygons(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL,
    index_name        TEXT NOT NULL,
    date              TIMESTAMPTZ NOT NULL,
    source            TEXT NOT NULL,   -- 'interpolated' | 'provisional'
    storage_path      TEXT NOT NULL,
    left_product_id   UUID NOT NULL REFERENCES catalog_products(id) ON DELETE CASCADE,
    right_product_id  UUID REFERENCES catalog_products(id) ON DELETE CASCADE,  -- NULL for provisional
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_interpolated_tile_polygon_platform_index_date
        UNIQUE (polygon_id, platform, index_name, date)
);

CREATE INDEX IF NOT EXISTS ix_interpolated_tiles_lookup
    ON interpolated_tiles (polygon_id, platform, index_name, date);