-- reset_db_keep_one_farm.sql -- ONE-OFF, DESTRUCTIVE, NOT idempotent-safe
-- to re-run casually. Keeps exactly one user (by email) and exactly one
-- farm (by name + that user), deletes everything else across every table.
--
-- BEFORE RUNNING THIS:
--   1. Back up the database. This cannot be undone otherwise.
--        pg_dump -U postgres vyom > vyom_backup_$(date +%Y%m%d_%H%M%S).sql
--   2. Run the PREVIEW section below FIRST (it's read-only) and confirm the
--      counts/IDs look right before touching the DELETE section at all.
--
-- Usage:
--   psql -U postgres -d vyom -f scripts/reset_db_keep_one_farm.sql
-- This runs inside one transaction and STOPS at the COMMIT/ROLLBACK choice
-- at the bottom -- read the NOTICE output between BEGIN and COMMIT, and
-- edit the last line to ROLLBACK instead of COMMIT if anything looks wrong.

\set keep_email 'iamsraj05@gmail.com'
\set keep_farm_name 'Bhopal - Vaishali Plot'

-- ===================================================================
-- PREVIEW (read-only) -- run this block alone first, separately, and look
-- at the output before running anything below it.
-- ===================================================================
SELECT id, email, account_id, name FROM users WHERE email = :'keep_email';

SELECT id, name, user_id, created_at
FROM polygons
WHERE name = :'keep_farm_name'
  AND user_id = (SELECT id FROM users WHERE email = :'keep_email');
-- ^ This MUST return exactly one row, with the user_id matching the user
-- row above. If it returns zero rows or more than one, STOP -- do not run
-- the DELETE section below until you know exactly which polygon id you're
-- keeping (adjust the WHERE clause below to match on id instead of name
-- if there's any ambiguity).

SELECT count(*) AS other_users FROM users WHERE email != :'keep_email';
SELECT count(*) AS other_farms FROM polygons
WHERE NOT (name = :'keep_farm_name'
           AND user_id = (SELECT id FROM users WHERE email = :'keep_email'));
SELECT count(*) AS total_error_logs FROM error_logs;
SELECT count(*) AS total_otp_rows FROM otp_verifications;

-- ===================================================================
-- DELETE (destructive) -- only run past this point once the preview above
-- looks correct.
-- ===================================================================
BEGIN;

-- Fully transient / operational data -- no reason to keep any of it.
DELETE FROM error_logs;
DELETE FROM otp_verifications;

-- Every farm except the one being kept. CASCADEs to zonal_stats,
-- interpolated_stats, interpolated_tiles, and polygon_tile_map for each
-- deleted farm automatically (all declared ON DELETE CASCADE from
-- polygon_id in schema.sql/migrations).
DELETE FROM polygons
WHERE NOT (
    name = :'keep_farm_name'
    AND user_id = (SELECT id FROM users WHERE email = :'keep_email')
);

-- Satellite scene records no longer referenced by anything -- everything
-- that referenced them via a deleted farm was already cascade-deleted
-- above, so what's left here is only ever what the KEPT farm still needs.
DELETE FROM catalog_products
WHERE id NOT IN (SELECT product_id FROM polygon_tile_map)
  AND id NOT IN (SELECT product_id FROM zonal_stats)
  AND id NOT IN (SELECT left_product_id FROM interpolated_tiles)
  AND id NOT IN (SELECT right_product_id FROM interpolated_tiles WHERE right_product_id IS NOT NULL);

-- Every user account except the one being kept.
DELETE FROM users WHERE email != :'keep_email';

-- ---- Sanity check before you decide COMMIT vs ROLLBACK ----
SELECT (SELECT count(*) FROM users) AS users_left,
       (SELECT count(*) FROM polygons) AS farms_left,
       (SELECT count(*) FROM catalog_products) AS products_left,
       (SELECT count(*) FROM zonal_stats) AS zonal_stats_left,
       (SELECT count(*) FROM error_logs) AS error_logs_left;
-- Expect: users_left = 1, farms_left = 1, error_logs_left = 0.
-- If that's what you see, uncomment COMMIT below (and leave ROLLBACK
-- commented). If anything looks wrong, leave ROLLBACK as-is and nothing
-- above takes effect.

-- COMMIT;
ROLLBACK;