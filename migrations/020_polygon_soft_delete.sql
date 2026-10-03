-- Soft delete for partner-API farms (DELETE /api/v1/farms/{id}).
-- RUN THIS BEFORE deploying the matching code: the Polygon model now selects
-- this column, so the API would error on every farm query without it.
ALTER TABLE polygons ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_polygons_active_by_user
    ON polygons (user_id) WHERE deleted_at IS NULL;