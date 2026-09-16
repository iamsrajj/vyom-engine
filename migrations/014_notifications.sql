-- Notifications: field_ready, new_reading, refresh_complete, stale_data,
-- contact_status, admin_alert. See vyom/notifications.py for the creation
-- logic and vyom/api/notifications.py for the read/list/mark-read API.
--
-- "context" (not "metadata") is deliberate: SQLAlchemy declarative models
-- reserve the attribute name `metadata` on every Base subclass (it's the
-- table-definition registry), so a column literally named metadata would
-- collide with that at the ORM layer. Same naming already used by
-- error_logs.context for the same reason.
CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    farm_id UUID REFERENCES polygons(id) ON DELETE CASCADE,
    type VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    body TEXT,
    context JSONB NOT NULL DEFAULT '{}',
    email_sent BOOLEAN NOT NULL DEFAULT false,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notifications_user_created
    ON notifications (user_id, created_at DESC);
-- Used to check "has this farm already gotten its field_ready notification"
-- and to throttle repeat notification types per farm.
CREATE INDEX IF NOT EXISTS idx_notifications_farm_type
    ON notifications (farm_id, type, created_at DESC);
-- Used to throttle admin_alert emails per error source (farm_id is NULL
-- for these, so the index above doesn't help admin_alert lookups).
CREATE INDEX IF NOT EXISTS idx_notifications_type_created
    ON notifications (type, created_at DESC);