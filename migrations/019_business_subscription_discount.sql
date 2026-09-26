-- 019_business_subscription_discount.sql
--
-- business_subscriptions.amount_paise was being overwritten with the
-- POST-discount amount at order-creation time (see the old
-- create_business_upgrade_order in vyom/api/billing.py), with nowhere to
-- record what the discount actually was -- so a coupon-discounted business
-- upgrade permanently lost its real base fee (e.g. an invoice showing
-- "Rs. 9.99" with no indication a coupon ever applied, when the real fee
-- is Rs. 999). This adds the missing column so amount_paise can go back to
-- always meaning the true pre-discount base (matching how farm_plans
-- already works), with discount_paise recording what was actually taken
-- off.
--
-- NOTE: this cannot retroactively recover the real base fee for rows
-- created before this fix -- their amount_paise already IS the
-- post-discount figure with no way to know what the original discount
-- was. Existing rows default discount_paise to 0, which is accurate for
-- any business subscription that never had a coupon applied, but for an
-- already-discounted historical row this will just make its (already
-- wrong) amount_paise look like the base with no discount shown -- same
-- display as before this fix for those specific rows, not worse. Only
-- new upgrades/renewals after this migration + the matching code change
-- get it right end to end.

ALTER TABLE business_subscriptions
    ADD COLUMN IF NOT EXISTS discount_paise INTEGER NOT NULL DEFAULT 0;