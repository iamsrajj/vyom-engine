-- Centralized error log -- every task/pipeline/API failure writes here so the
-- dashboard's Errors panel is one query instead of grepping journalctl across
-- discovery/download/process/zonal-stats/API/auth separately.
-- Run this against the existing DB: psql "$DATABASE_URL" -f migrations/002_error_logs.sql

CREATE TABLE IF NOT EXISTS error_logs (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,             -- e.g. 'tasks.refresh_farm', 'download_manager', 'pipeline_s1', 'pipeline_s2', 'zonal_stats', 'discovery', 'api.farms', 'auth'
    platform    TEXT,                       -- 'S2' / 'S1' / null for non-platform sources
    level       TEXT NOT NULL DEFAULT 'error',  -- 'error' | 'warning'
    message     TEXT NOT NULL,
    traceback   TEXT,
    context     JSONB NOT NULL DEFAULT '{}',    -- farm_id, product_id, product_name, request path, etc.
    resolved    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_error_logs_created_at ON error_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_error_logs_source ON error_logs (source);
CREATE INDEX IF NOT EXISTS ix_error_logs_resolved ON error_logs (resolved) WHERE resolved = FALSE;