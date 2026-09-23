-- scripts/cleanup_draft_farm_notifications.sql
--
-- One-off cleanup for notifications that were sent about draft/prewarm-seed
-- farms before the fix in vyom/tasks.py (fill_gaps_callback now skips
-- notify_farm_data_update/notify_refresh_complete for farm.is_draft or
-- farm.is_prewarm_seed). These reference a "field" the farmer never sees
-- in their farm list, so they're just confusing noise -- e.g. "New reading
-- available for Untitled field (drawing...)".
--
-- Preview first (run this, check the row count/content looks right):
SELECT n.id, n.type, n.title, n.body, n.created_at, p.name, p.is_draft, p.is_prewarm_seed
FROM notifications n
JOIN polygons p ON p.id = n.farm_id
WHERE n.type IN ('field_ready', 'new_reading', 'refresh_complete')
  AND (p.is_draft = true OR p.is_prewarm_seed = true);

-- Once the preview above looks right, actually delete them:
-- DELETE FROM notifications
-- WHERE id IN (
--     SELECT n.id
--     FROM notifications n
--     JOIN polygons p ON p.id = n.farm_id
--     WHERE n.type IN ('field_ready', 'new_reading', 'refresh_complete')
--       AND (p.is_draft = true OR p.is_prewarm_seed = true)
-- );