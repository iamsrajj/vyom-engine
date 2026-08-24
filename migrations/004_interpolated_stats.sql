-- Gap-filled points on a fixed cadence, kept fully separate from zonal_stats
-- (the real satellite readings). Run: psql "$DATABASE_URL" -f migrations/004_interpolated_stats.sql

CREATE TABLE IF NOT EXISTS interpolated_stats (
    id                    BIGSERIAL PRIMARY KEY,
    polygon_id            UUID NOT NULL REFERENCES polygons(id) ON DELETE CASCADE,
    platform              TEXT NOT NULL,   -- 'S1' or 'S2'
    metric                TEXT NOT NULL,
    date                  TIMESTAMPTZ NOT NULL,
    value                 NUMERIC,
    method                TEXT NOT NULL DEFAULT 'linear',
    left_zonal_stat_id    BIGINT NOT NULL REFERENCES zonal_stats(id) ON DELETE CASCADE,
    right_zonal_stat_id   BIGINT NOT NULL REFERENCES zonal_stats(id) ON DELETE CASCADE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_interpolated_polygon_metric_date UNIQUE (polygon_id, metric, date)
);

CREATE INDEX IF NOT EXISTS ix_interpolated_stats_polygon_metric
    ON interpolated_stats (polygon_id, metric, date);