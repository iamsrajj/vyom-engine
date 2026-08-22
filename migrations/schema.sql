-- Vyom Engine schema -- Sentinel-1 + Sentinel-2, multi-index (JSONB), multi-region.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Catalog of every discovered/downloaded/processed satellite product (S1 or S2).
CREATE TABLE IF NOT EXISTS catalog_products (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform            TEXT NOT NULL DEFAULT 'S2',        -- 'S2' optical or 'S1' SAR
    collection          TEXT NOT NULL DEFAULT 'SENTINEL-2',
    product_id          TEXT UNIQUE NOT NULL,               -- Copernicus product Id (UUID)
    product_name        TEXT NOT NULL,
    tile_id             TEXT,
    acquisition_date    TIMESTAMPTZ NOT NULL,
    cloud_cover         NUMERIC,                            -- null/meaningless for S1
    footprint           GEOMETRY(Polygon, 4326) NOT NULL,
    status              TEXT NOT NULL DEFAULT 'discovered',  -- discovered -> downloaded -> processed -> failed
    raw_path            TEXT,
    processed_indices   JSONB NOT NULL DEFAULT '{}',         -- {"NDVI": "s3://.../ndvi.tif", ...}
    checksum            TEXT,
    error_message       TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_catalog_footprint ON catalog_products USING GIST (footprint);
CREATE INDEX IF NOT EXISTS idx_catalog_tile_date ON catalog_products (tile_id, acquisition_date);
CREATE INDEX IF NOT EXISTS idx_catalog_status ON catalog_products (status);
CREATE INDEX IF NOT EXISTS idx_catalog_platform ON catalog_products (platform);

-- Farm/field polygons -- can be anywhere in the world, not tied to one region.
CREATE TABLE IF NOT EXISTS polygons (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    user_id     UUID NOT NULL,
    name        TEXT,
    geom        GEOMETRY(Polygon, 4326) NOT NULL,
    area_ha     NUMERIC,
    crop_type   TEXT,
    country     TEXT,
    sowing_date DATE,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_polygons_geom ON polygons USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_polygons_user ON polygons (user_id);

-- Precomputed farm <-> product intersections
CREATE TABLE IF NOT EXISTS polygon_tile_map (
    polygon_id  UUID REFERENCES polygons(id) ON DELETE CASCADE,
    product_id  UUID REFERENCES catalog_products(id) ON DELETE CASCADE,
    PRIMARY KEY (polygon_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_ptm_product ON polygon_tile_map (product_id);

-- Zonal statistics: one row per farm per product per metric (e.g. 'NDVI_mean').
CREATE TABLE IF NOT EXISTS zonal_stats (
    id               BIGSERIAL PRIMARY KEY,
    polygon_id       UUID NOT NULL REFERENCES polygons(id) ON DELETE CASCADE,
    product_id       UUID NOT NULL REFERENCES catalog_products(id) ON DELETE CASCADE,
    acquisition_date TIMESTAMPTZ NOT NULL,
    metric           TEXT NOT NULL,
    value            DOUBLE PRECISION,
    pixel_count      INT,
    cloud_pct        NUMERIC,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_zstats_polygon_date ON zonal_stats (polygon_id, acquisition_date);
CREATE INDEX IF NOT EXISTS idx_zstats_polygon_metric ON zonal_stats (polygon_id, metric);
CREATE UNIQUE INDEX IF NOT EXISTS uq_zstats_polygon_product_metric ON zonal_stats (polygon_id, product_id, metric);

-- ===================================================================
-- Migrating an existing Phase-1 database (old schema had fixed columns
-- processed_ndvi_path/processed_ndwi_path instead of processed_indices JSONB)?
-- Run this instead of the CREATE TABLE above:
--
-- ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'S2';
-- ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS processed_indices JSONB NOT NULL DEFAULT '{}';
-- UPDATE catalog_products SET processed_indices = jsonb_strip_nulls(jsonb_build_object(
--     'NDVI', processed_ndvi_path, 'NDWI', processed_ndwi_path))
--   WHERE processed_ndvi_path IS NOT NULL OR processed_ndwi_path IS NOT NULL;
-- ALTER TABLE catalog_products DROP COLUMN IF EXISTS processed_ndvi_path;
-- ALTER TABLE catalog_products DROP COLUMN IF EXISTS processed_ndwi_path;
-- ALTER TABLE polygons ADD COLUMN IF NOT EXISTS country TEXT;
-- ALTER TABLE polygons ADD COLUMN IF NOT EXISTS sowing_date DATE;
-- CREATE INDEX IF NOT EXISTS idx_catalog_platform ON catalog_products (platform);
-- ===================================================================