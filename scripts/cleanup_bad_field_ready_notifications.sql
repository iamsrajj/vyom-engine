-- cleanup_bad_field_ready_notifications.sql -- OPTIONAL, one-off.
--
-- Before the fix in tasks.py/notifications.py, every pre-existing farm's
-- first refresh after the notification system shipped incorrectly fired a
-- "field_ready" notification (in-app + email), because the notifications
-- table starts empty for every farm regardless of age -- there was no way
-- to tell "genuinely new farm" from "old farm, just never notified before".
--
-- This is now fixed going forward (had_data_before is computed from the
-- farm's actual zonal_stats history, not notification history). This
-- script only cleans up the incorrect notifications that already got
-- created/emailed by the old logic -- purely cosmetic, safe to skip
-- entirely if you don't care about the stale panel entries.
--
-- Heuristic: a field_ready notification is "wrong" if the farm already had
-- a REAL reading dated more than a day before that notification was
-- created -- i.e. it demonstrably wasn't actually the farm's first reading.

-- ===================== PREVIEW (read-only) — run this first =====================
SELECT n.id, n.title, n.created_at, p.name AS farm_name
FROM notifications n
JOIN polygons p ON p.id = n.farm_id
WHERE n.type = 'field_ready'
  AND EXISTS (
    SELECT 1 FROM zonal_stats z
    WHERE z.polygon_id = n.farm_id
      AND z.value IS NOT NULL
      AND z.acquisition_date < n.created_at - INTERVAL '1 day'
  );
-- Check this list looks right (should be exactly the farms you saw the
-- incorrect notification for) before running the DELETE below.

-- ===================== DELETE (only after reviewing the preview) =====================
-- DELETE FROM notifications n
-- WHERE n.type = 'field_ready'
--   AND EXISTS (
--     SELECT 1 FROM zonal_stats z
--     WHERE z.polygon_id = n.farm_id
--       AND z.value IS NOT NULL
--       AND z.acquisition_date < n.created_at - INTERVAL '1 day'
--   );