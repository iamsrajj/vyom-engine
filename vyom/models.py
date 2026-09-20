import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    Column, String, Numeric, DateTime, Date, Text, ForeignKey, BigInteger, Integer, UniqueConstraint, Boolean, Float
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import relationship

from vyom.db import Base


class CatalogProduct(Base):
    __tablename__ = "catalog_products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # "S2" (optical) or "S1" (SAR)
    platform = Column(String, nullable=False, default="S2")
    collection = Column(String, nullable=False, default="SENTINEL-2")
    product_id = Column(String, unique=True, nullable=False)
    product_name = Column(String, nullable=False)
    tile_id = Column(String)
    acquisition_date = Column(DateTime(timezone=True), nullable=False)
    cloud_cover = Column(Numeric)  # null/meaningless for S1
    footprint = Column(
        Geometry(geometry_type="POLYGON", srid=4326), nullable=False)
    # The ACTUAL windowed extent that was read/written when this product was
    # processed (see pipeline_s2.py/pipeline_s1.py) -- NOT the same as
    # `footprint` above, which is the full satellite scene footprint. A farm
    # can spatially intersect `footprint` while falling completely outside
    # the narrower window that was actually processed (farms_bounds_for_product
    # windows to whichever farms were linked AT PROCESSING TIME -- a farm
    # linked later isn't automatically covered). NULL for older rows
    # processed before this column existed, or for products not yet
    # processed -- reuse_check.py must treat NULL as "no known coverage".
    processed_bounds = Column(Geometry(geometry_type="POLYGON", srid=4326))
    # Overrides tile_grid.farms_bounds_for_product's default buffer for THIS
    # product's window, set once at discovery time for a genuine cold-start
    # farm (see reuse_check.py + settings.cold_start_buffer_deg). NULL means
    # "use the normal default" -- read once, at first processing, by
    # pipeline_s1.py/pipeline_s2.py; irrelevant after the window is fixed
    # (an already-processed product's extent can't be retroactively resized
    # without reprocessing, same as processed_bounds above).
    cold_start_buffer_deg = Column(Numeric)
    status = Column(String, nullable=False, default="discovered")
    raw_path = Column(Text)

    # One row per index -> COG path, e.g. {"NDVI": "s3://.../NDVI.tif", "NDWI": "..."}.
    # JSONB rather than one DB column per index means adding a new index later is a
    # code change only -- no migration required.
    processed_indices = Column(JSONB, nullable=False, server_default="{}")

    checksum = Column(Text)
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True),
                        default=datetime.utcnow, onupdate=datetime.utcnow)


class Polygon(Base):
    __tablename__ = "polygons"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), nullable=False,
                       default=uuid.UUID(int=0))
    user_id = Column(UUID(as_uuid=True), nullable=False)
    name = Column(String)
    geom = Column(Geometry(geometry_type="POLYGON", srid=4326), nullable=False)
    area_ha = Column(Numeric)
    crop_type = Column(String)
    soil_type = Column(String)
    # populated at creation from reverse geocoding or client-supplied
    country = Column(String)
    # crop age is always derived as (today - sowing_date), never stored
    sowing_date = Column(Date)
    # True for a rough placeholder polygon created immediately from a
    # map-pin/village selection, before the farmer has finished tracing the
    # real boundary -- see the parallel-fetch-during-drawing flow in
    # farms.py (create_farm with is_draft=True, then update_farm with the
    # real geometry to finalize). A draft still gets the full reuse-check +
    # priority cold-start fetch immediately, same as any other farm -- the
    # flag is purely a lifecycle/display marker (e.g. list_farms excludes
    # drafts by default), it does not change fetch behavior at all.
    is_draft = Column(Boolean, nullable=False, server_default="false")
    # A purely synthetic seed created by the prewarm tool (vyom/prewarm.py)
    # to trigger coverage for a region ahead of any real farmer -- NEVER a
    # real farm, unlike is_draft above. Its only purpose is to exist long
    # enough for its refresh to complete and leave real processed_bounds
    # coverage behind on the CatalogProduct rows it caused to be processed
    # -- that coverage persists independently of this seed polygon (reuse-
    # check queries processed_bounds directly, not "is a farm still linked
    # here"), so these rows can be deleted later without undoing the
    # benefit. Always excluded from list_farms, same as is_draft.
    is_prewarm_seed = Column(Boolean, nullable=False, server_default="false")

    # 'dashboard' (default) or 'api'. Drives the whole individual-vs-business
    # pricing/feature split -- see FarmPlan and BusinessApiInvoice below.
    # NEVER derived from account_type: a business account's OWN
    # dashboard-created farms are still 'dashboard'/'full', exactly like an
    # individual's -- only farms actually created through the (not yet
    # built) partner API get 'api'/'indices_only'. Keeping this on the farm
    # itself, not computed from the owner's account_type, is what prevents
    # the two surfaces (dashboard rendering, future API responses) from
    # ever silently drifting out of sync on which farms get which features.
    created_via = Column(String, nullable=False, server_default="dashboard")
    # 'full' (all satellite indices + Hyperlocal Weather + Gyan AI Expanded
    # + farm-based dynamic advisory) or 'indices_only' (satellite indices
    # only). Every place that decides whether to show/return weather, Gyan
    # AI, or advisory for a farm MUST check this field, not created_via or
    # account_type directly -- this is the one flag both the dashboard and
    # any future API surface read.
    feature_tier = Column(String, nullable=False, server_default="full")

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class FarmPlan(Base):
    """One row per individual farm-plan purchase/renewal/upgrade attempt --
    same 'ledger of attempts, not just the current state' shape as
    BusinessSubscription, and for the same reason (abandoned checkouts stay
    visible as status='created', never deleted).

    A farm's CURRENT lock state is derived by looking up this farm's most
    recent row with status='active' and checking expires_at against now --
    see vyom/farm_pricing.py's is_farm_locked(), which is the ONLY function
    that should make that determination (every gated endpoint calls it,
    rather than re-deriving the lock logic inline in multiple places).

    Upgrading a farm's plan does NOT delete or edit the old row -- it marks
    the old row status='upgraded' and creates a new one, so the purchase
    history (and the exact proration credit given) stays fully auditable.
    """
    __tablename__ = "farm_plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    farm_id = Column(UUID(as_uuid=True), ForeignKey(
        "polygons.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey(
        "users.id", ondelete="CASCADE"), nullable=False)

    # 'individual_3m' | 'individual_6m' | 'individual_12m'
    plan_type = Column(String, nullable=False)
    duration_days = Column(Integer, nullable=False)
    # Area at the moment of purchase -- kept even though Polygon.area_ha can
    # change later (a redrawn boundary), so a past invoice always shows what
    # was actually charged, not today's area.
    area_acre_snapshot = Column(Numeric, nullable=False)
    rate_per_acre_paise = Column(Integer, nullable=False)

    # area * rate, pre-discount
    base_paise = Column(Integer, nullable=False)
    discount_paise = Column(Integer, nullable=False, server_default="0")
    # Credit from an old plan's unused remaining value, applied toward this
    # purchase -- only set on an upgrade, see vyom/farm_pricing.py.
    proration_credit_paise = Column(
        Integer, nullable=False, server_default="0")
    gst_paise = Column(Integer, nullable=False, server_default="0")
    wallet_applied_paise = Column(Integer, nullable=False, server_default="0")
    # What Razorpay actually needs to charge = base - discount - proration
    # - wallet + gst. Can be 0 if wallet/proration fully covers it, in which
    # case razorpay_order_id stays NULL and the plan activates immediately.
    razorpay_paise = Column(Integer, nullable=False, server_default="0")

    coupon_id = Column(UUID(as_uuid=True), ForeignKey("coupons.id"))
    # Set only on an upgrade -- see vyom/farm_pricing.py's upgrade_plan().
    # Persisted here (not passed around as a function parameter) because
    # activation can happen much later, asynchronously, via the Razorpay
    # webhook -- the link has to survive that round trip on the row itself.
    upgraded_from_plan_id = Column(
        UUID(as_uuid=True), ForeignKey("farm_plans.id"))
    razorpay_order_id = Column(String, unique=True)
    razorpay_payment_id = Column(String, unique=True)

    # created (awaiting payment, or awaiting nothing if razorpay_paise=0 --
    # see activate_farm_plan, which is called immediately in that case) ->
    # active -> expired | upgraded. failed if Razorpay reported failure.
    status = Column(String, nullable=False, server_default="created")

    starts_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True),
                        default=datetime.utcnow, onupdate=datetime.utcnow)


class BusinessApiInvoice(Base):
    """One row per calendar month per business account, covering every farm
    that account's API key(s) created THAT month (not farms created in
    earlier months -- each month's cohort gets its own invoice, matching
    'billed in the next month for all farms created by API in that month').

    This table exists now, ahead of the API-key platform itself, so the
    monthly billing job (vyom/billing_tasks.py) is ready the moment farms
    start getting created_via='api' -- until then, generate_monthly_
    business_api_invoices() simply finds zero qualifying farms per account
    and creates nothing.
    """
    __tablename__ = "business_api_invoices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey(
        "users.id", ondelete="CASCADE"), nullable=False)
    # First day of the billed month, e.g. 2026-09-01 for September's farms.
    billing_month = Column(Date, nullable=False)

    total_area_acre = Column(Numeric, nullable=False)
    rate_per_acre_paise = Column(Integer, nullable=False)
    base_paise = Column(Integer, nullable=False)
    gst_paise = Column(Integer, nullable=False, server_default="0")
    total_paise = Column(Integer, nullable=False)

    razorpay_payment_link_id = Column(String, unique=True)
    razorpay_payment_link_url = Column(String)
    razorpay_payment_id = Column(String, unique=True)

    issued_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    due_at = Column(DateTime(timezone=True),
                    nullable=False)  # issued_at + 15 days

    # pending -> paid, or pending -> overdue (past due_at/15-day grace,
    # unpaid -- vyom/billing_tasks.py's daily check sets this AND flips the
    # owning User.business_api_payment_status to 'suspended' in the same
    # transaction, so the two never disagree about whether the account is
    # currently restricted).
    status = Column(String, nullable=False, server_default="pending")

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True),
                        default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "billing_month",
                         name="uq_business_invoice_user_month"),
    )


class BusinessApiInvoiceFarm(Base):
    """Junction row: which farms (and what area) a given monthly invoice
    actually covers. Recorded explicitly at invoice-generation time rather
    than re-derived later from Polygon.area_ha, so a farm's boundary being
    redrawn afterwards never silently changes a past invoice's total."""
    __tablename__ = "business_api_invoice_farms"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey(
        "business_api_invoices.id", ondelete="CASCADE"), nullable=False)
    farm_id = Column(UUID(as_uuid=True), ForeignKey(
        "polygons.id", ondelete="CASCADE"), nullable=False)
    area_acre_snapshot = Column(Numeric, nullable=False)


class PolygonTileMap(Base):
    __tablename__ = "polygon_tile_map"

    polygon_id = Column(UUID(as_uuid=True), ForeignKey(
        "polygons.id", ondelete="CASCADE"), primary_key=True)
    product_id = Column(UUID(as_uuid=True), ForeignKey(
        "catalog_products.id", ondelete="CASCADE"), primary_key=True)


class ZonalStat(Base):
    __tablename__ = "zonal_stats"
    __table_args__ = (
        UniqueConstraint("polygon_id", "product_id", "metric",
                         name="uq_zstats_polygon_product_metric"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    polygon_id = Column(UUID(as_uuid=True), ForeignKey(
        "polygons.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey(
        "catalog_products.id", ondelete="CASCADE"), nullable=False)
    acquisition_date = Column(DateTime(timezone=True), nullable=False)
    metric = Column(String, nullable=False)  # e.g. 'NDVI_mean', 'RVI_std'
    value = Column(Numeric)
    pixel_count = Column(Integer)
    cloud_pct = Column(Numeric)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class InterpolatedStat(Base):
    """Gap-filled / provisionally-filled points on a fixed cadence (default 6
    days). Two distinct kinds, distinguished by `source`:

      source="interpolated": computed BETWEEN two real zonal_stats readings
      (linear interpolation) -- never before the first or after the last real
      observation for a polygon+metric. right_zonal_stat_id is set.

      source="provisional": only ONE real anchor exists so far (the most
      recent real reading) and no second real reading has arrived yet --
      flat carry-forward of that single anchor's value, clearly weaker than
      a true interpolation since there's nothing on the other side to draw a
      line to. right_zonal_stat_id is NULL. These rows are TEMPORARY: the
      moment a real second reading arrives, fill_gaps_for_polygon() deletes
      every provisional row in that now-closed gap and replaces them with
      properly interpolated ones -- provisional data must never linger next
      to a gap that's since become fillable for real.

    Deliberately a SEPARATE table from zonal_stats, not an extra column/flag
    on it: zonal_stats stays a pure record of what the satellites actually
    measured. Every row here traces back to its real zonal_stats anchor(s),
    so a caller can always verify provenance instead of trusting the number
    blind. Every API response that includes these MUST carry the `source`
    distinction -- see ZonalStatOut.source in farms.py. Never collapse
    "satellite" / "interpolated" / "provisional" into one undifferentiated
    "data" field when displaying to a farmer."""
    __tablename__ = "interpolated_stats"
    __table_args__ = (
        UniqueConstraint("polygon_id", "metric", "date",
                         name="uq_interpolated_polygon_metric_date"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    polygon_id = Column(UUID(as_uuid=True), ForeignKey(
        "polygons.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String, nullable=False)  # 'S1' or 'S2'
    metric = Column(String, nullable=False)
    date = Column(DateTime(timezone=True), nullable=False)
    value = Column(Numeric)
    # "interpolated" (two real anchors) or "provisional" (one real anchor,
    # flat carry-forward, pending a second real reading to confirm/replace it)
    source = Column(String, nullable=False, default="interpolated")
    # "linear" or "carry_forward"
    method = Column(String, nullable=False, default="linear")
    left_zonal_stat_id = Column(BigInteger, ForeignKey(
        "zonal_stats.id", ondelete="CASCADE"), nullable=False)
    # NULL for provisional rows -- there is no right anchor yet, that's the
    # whole point of "provisional".
    right_zonal_stat_id = Column(BigInteger, ForeignKey(
        "zonal_stats.id", ondelete="CASCADE"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class InterpolatedTile(Base):
    """The pixel-level (raster) counterpart to InterpolatedStat's scalar
    per-farm numbers. Same source distinction and same supersede rule apply
    -- see InterpolatedStat's docstring for the full explanation.

    IMPORTANT storage optimization: a "provisional" row does NOT get its own
    COG file. Provisional is a flat carry-forward of the single most recent
    real raster, unchanged pixel-for-pixel -- so storage_path just points at
    that SAME real product's existing COG (right_product_id is NULL, no new
    file, zero extra Wasabi storage). Only "interpolated" rows (two real
    anchors, genuine pixel-wise linear interpolation) get a real newly
    written COG file of their own -- see raster_interpolation.py.

    When a provisional row gets superseded by real interpolation, only the
    DB row is deleted (it never owned a separate file). When an
    "interpolated" row's underlying real data is deleted for spatial reasons
    the storage layer would need real cleanup -- not expected in normal
    operation, so not handled automatically here."""
    __tablename__ = "interpolated_tiles"
    __table_args__ = (
        UniqueConstraint("polygon_id", "platform", "index_name", "date",
                         name="uq_interpolated_tile_polygon_platform_index_date"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    polygon_id = Column(UUID(as_uuid=True), ForeignKey(
        "polygons.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String, nullable=False)
    # e.g. "NDVI", "RVI" -- named _name to avoid shadowing SQL INDEX
    index_name = Column(String, nullable=False)
    date = Column(DateTime(timezone=True), nullable=False)
    source = Column(String, nullable=False)  # "interpolated" or "provisional"
    storage_path = Column(Text, nullable=False)
    left_product_id = Column(UUID(as_uuid=True), ForeignKey(
        "catalog_products.id", ondelete="CASCADE"), nullable=False)
    right_product_id = Column(UUID(as_uuid=True), ForeignKey(
        "catalog_products.id", ondelete="CASCADE"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class ErrorLog(Base):
    """Single place every failure across every task/module/API route gets
    written to, so the dashboard's Errors panel is one query instead of
    grepping journalctl across discovery/download/process/zonal-stats/API.
    Deliberately flat (no FK constraints on farm_id/product_id) since a
    logging path must never itself fail because a farm was since deleted."""
    __tablename__ = "error_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    # e.g. "tasks.refresh_farm", "download_manager", "pipeline_s1",
    # "pipeline_s2", "zonal_stats", "discovery", "api.farms", "auth"
    source = Column(String, nullable=False)
    # e.g. "S2", "S1", or null for non-platform sources (API/auth)
    platform = Column(String)
    level = Column(String, nullable=False, default="error")  # error | warning
    message = Column(Text, nullable=False)
    traceback = Column(Text)
    # free-form context: farm_id, product_id, product_name, request path, etc.
    context = Column(JSONB, nullable=False, server_default="{}")
    resolved = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Notification(Base):
    """In-app + emailed notifications -- field_ready, new_reading,
    refresh_complete, stale_data, contact_status, admin_alert. See
    vyom/notifications.py for creation logic (including per-type email
    behavior) and vyom/api/notifications.py for the list/read API this backs.

    Column named `context`, not `metadata` -- `metadata` is reserved on every
    SQLAlchemy declarative Base subclass (the table-registry attribute), so a
    real column with that name would collide with it. Same reason
    ErrorLog.context is named the way it is."""
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey(
        "users.id", ondelete="CASCADE"), nullable=False)
    farm_id = Column(UUID(as_uuid=True), ForeignKey(
        "polygons.id", ondelete="CASCADE"))
    # field_ready | new_reading | refresh_complete | stale_data | contact_status | admin_alert
    type = Column(String, nullable=False)
    title = Column(String, nullable=False)
    body = Column(Text)
    context = Column(JSONB, nullable=False, server_default="{}")
    email_sent = Column(Boolean, nullable=False, server_default="false")
    read_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class User(Base):
    """A real account -- replaces the flat AUTH_USERS env-var list. Every
    account starts with Google sign-in (name/email/picture come from Google's
    verified ID token, never trusted from the client directly) then completes
    a profile (org/designation/address/phone) with the phone verified by OTP
    before the account is usable."""
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Short human-friendly identifier shown in the UI/support conversations,
    # e.g. "AGD-7F3K2Q" -- distinct from the internal uuid `id`.
    account_id = Column(String, unique=True, nullable=False)

    email = Column(String, unique=True, nullable=True)
    # Google's stable user id -- null until/unless the account links Google
    google_sub = Column(String, unique=True, nullable=True)
    name = Column(String, nullable=False)
    profile_img_url = Column(Text)

    organization = Column(String, nullable=False)
    designation = Column(String, nullable=False)
    address = Column(Text, nullable=False)

    phone_cc = Column(String, nullable=False, default="91")
    phone = Column(String, unique=True, nullable=False)
    phone_verified = Column(Boolean, nullable=False, server_default="false")
    # 'user' (default) or 'admin'. Gates the Errors panel and prewarm tool
    # (see auth.py's require_error_panel_access) -- fails closed, no role
    # means no access. Promoting the first admin is a one-time manual step:
    # UPDATE users SET role = 'admin' WHERE email = '<you>';
    role = Column(String, nullable=False, server_default="user")

    # 'individual' (default) or 'business'. Business unlocks API credential
    # issuance (see ApiCredential) once business_status='active' -- see
    # vyom/api/billing.py for the Razorpay upgrade flow that sets these.
    account_type = Column(String, nullable=False, server_default="individual")
    # 'active' | 'expired' | NULL (never been a business account). A lapsed
    # business subscription restricts API access ONLY -- the dashboard,
    # farms, and their data are completely unaffected either way. See
    # BusinessSubscription for the payment history behind this flag.
    business_status = Column(String)
    business_expires_at = Column(DateTime(timezone=True))
    # 'current' (default) | 'overdue' (past a monthly invoice's due_at, one
    # or more reminders sent) | 'suspended' (grace period lapsed). Read by
    # the future API-key auth gate (see vyom/api/billing.py's spec notes) --
    # kept on User rather than waiting for ApiCredential to exist, since the
    # monthly invoicing job (vyom/billing_tasks.py) needs somewhere to
    # record this state regardless of whether the API platform has shipped
    # yet. Never affects business_status/dashboard access -- see the "lapsed
    # business invoice restricts API access only" decision.
    business_api_payment_status = Column(
        String, nullable=False, server_default="current")

    # Denormalized running total, kept in sync with wallet_transactions in
    # the SAME db transaction as every insert there (see vyom/wallet.py) --
    # never computed on the fly from the ledger, so checkout can read it
    # with a single cheap column access.
    wallet_balance_paise = Column(Integer, nullable=False, server_default="0")

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True),
                        default=datetime.utcnow, onupdate=datetime.utcnow)


class BusinessSubscription(Base):
    """One row per ₹999/year business-maintenance payment attempt (not one
    row per active year -- 'created' rows for abandoned checkouts are kept,
    not deleted, so the payment history is a complete audit trail). The
    CURRENT state of a user's business access lives on User.business_status
    / business_expires_at -- this table is the ledger that produced it, in
    the same spirit as zonal_stats vs interpolated_stats: one place records
    what actually happened (Razorpay orders/payments), a denormalized field
    elsewhere answers "is it active right now" cheaply.

    Renewal is manual for now (a reminder email with a Checkout link, not an
    auto-charge/e-mandate) -- see vyom/api/billing.py's renewal reminder job.
    """
    __tablename__ = "business_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey(
        "users.id", ondelete="CASCADE"), nullable=False)

    amount_paise = Column(Integer, nullable=False)      # base, pre-GST
    gst_paise = Column(Integer, nullable=False, server_default="0")
    # amount + gst, what Razorpay actually charged
    total_paise = Column(Integer, nullable=False)

    razorpay_order_id = Column(String, unique=True, nullable=False)
    razorpay_payment_id = Column(String, unique=True)     # set once paid
    razorpay_signature = Column(String)

    # created -> paid (webhook/verify confirmed) or failed (Razorpay said so)
    status = Column(String, nullable=False, server_default="created")

    starts_at = Column(DateTime(timezone=True))
    ends_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True),
                        default=datetime.utcnow, onupdate=datetime.utcnow)


class WalletTransaction(Base):
    """Append-only ledger backing User.wallet_balance_paise. Every row here
    MUST be written in the same db.commit() as whatever caused it (a coupon
    redemption, a farm-plan purchase spending the balance down, an admin
    adjustment) -- see vyom/wallet.py, which is the ONLY place that should
    ever write to this table or to User.wallet_balance_paise, so the two
    never drift apart.

    amount_paise is signed: positive = credit (cashback, wallet-credit
    coupon, referral bonus), negative = spend (applied toward a purchase at
    checkout). balance_after_paise is a point-in-time snapshot for audit/
    display -- never trust it over recomputing from the full ledger if the
    two ever disagree, but in normal operation they won't.
    """
    __tablename__ = "wallet_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey(
        "users.id", ondelete="CASCADE"), nullable=False)
    amount_paise = Column(Integer, nullable=False)
    # 'coupon_cashback' | 'coupon_credit' | 'farm_plan_spend' | 'referral' | 'admin_adjustment'
    reason = Column(String, nullable=False)
    # loosely-typed pointer at whatever caused this (coupon_redemptions.id,
    # a future farm_plans.id, etc.) -- deliberately no FK, same reasoning as
    # ErrorLog.context: a ledger row must never fail to write because the
    # thing it references was since deleted.
    reference_id = Column(UUID(as_uuid=True))
    balance_after_paise = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Coupon(Base):
    """Generalized coupon engine backing every type in the commercial coupon
    list (percentage/flat/tiered/spend-threshold/buy-x-get-y/cashback/
    wallet-credit discounts), with a separate set of eligibility columns that
    layer on top of ANY calculation type. This is deliberate: "First Order",
    "New User", "Account/User-Based", "Plan-Specific", "Product-Specific",
    "Category-Based", "One-Time", "Limited-Use", "Partner", and "Recurring"
    coupons aren't distinct calculations, they're eligibility constraints --
    modeling them as one flat set of columns (rather than 22 separate
    calculation branches) is what keeps this maintainable. See
    vyom/coupons.py's module docstring for the full mapping from the
    commercial names to (calculation_type + eligibility columns).
    """
    __tablename__ = "coupons"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String, unique=True, nullable=False)
    description = Column(Text)

    # 'percentage' | 'percentage_max_cap' | 'flat' | 'flat_min_order' |
    # 'buy_x_get_y' | 'buy_x_get_y_discounted' | 'tiered' | 'spend_x_get_y' |
    # 'free_shipping' | 'cashback' | 'wallet_credit'
    calculation_type = Column(String, nullable=False)

    # -- calculation parameters (only the ones relevant to calculation_type
    #    are populated; kept as plain nullable columns rather than one
    #    JSONB blob so admin tooling/validation can be straightforward) --
    percent = Column(Float)
    flat_paise = Column(Integer)
    max_discount_paise = Column(Integer)          # percentage_max_cap
    min_order_paise = Column(Integer)              # flat_min_order
    buy_qty = Column(Integer)                      # buy_x_get_y[_discounted]
    get_qty = Column(Integer)
    # buy_x_get_y_discounted (100 = fully free)
    get_discount_percent = Column(Float)
    # buy_x_get_y[_discounted] -- price of the unit being given/discounted
    unit_price_paise = Column(Integer)
    spend_threshold_paise = Column(Integer)         # spend_x_get_y
    # tiered: [{"min_paise": int, "percent": float}, ...]
    tiers = Column(JSONB)
    waived_charge_codes = Column(ARRAY(String))     # free_shipping/service

    # -- eligibility (apply on top of any calculation_type above) --
    # shown in the public coupon section
    is_public = Column(Boolean, nullable=False, server_default="false")
    # Account/User-Based, Partner
    eligible_user_ids = Column(ARRAY(UUID(as_uuid=True)))
    first_order_only = Column(Boolean, nullable=False, server_default="false")
    new_user_within_days = Column(Integer)          # New User Coupon
    # Plan-Specific, Subscription Discount
    eligible_plan_types = Column(ARRAY(String))
    eligible_products = Column(ARRAY(String))       # Product-Specific
    eligible_categories = Column(ARRAY(String))     # Category-Based
    requires_referral = Column(Boolean, nullable=False, server_default="false")
    # Recurring Coupon / Subscription Discount: applies to this many
    # consecutive redemptions by the SAME user (e.g. 3 renewal cycles).
    # NULL = no recurring limit beyond max_redemptions_per_user below.
    recurring_cycles = Column(Integer)

    max_redemptions = Column(Integer)               # global cap (Limited-Use)
    max_redemptions_per_user = Column(
        Integer, nullable=False, server_default="1")  # =1 gives One-Time behavior

    starts_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True))
    # active | disabled | expired
    status = Column(String, nullable=False, server_default="active")

    created_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True),
                        default=datetime.utcnow, onupdate=datetime.utcnow)


class CouponRedemption(Base):
    """One row per successful application of a coupon to an order. Used both
    to enforce max_redemptions/max_redemptions_per_user and as the audit
    trail for what discount was actually given on a specific charge."""
    __tablename__ = "coupon_redemptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    coupon_id = Column(UUID(as_uuid=True), ForeignKey(
        "coupons.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey(
        "users.id", ondelete="CASCADE"), nullable=False)
    # e.g. a business_subscriptions.id or (later) a farm_plans.id -- no FK,
    # same reasoning as WalletTransaction.reference_id.
    # 'business_subscription' | 'farm_plan'
    order_reference_type = Column(String, nullable=False)
    order_reference_id = Column(UUID(as_uuid=True), nullable=False)
    discount_paise = Column(Integer, nullable=False, server_default="0")
    wallet_credit_paise = Column(Integer, nullable=False, server_default="0")
    redeemed_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class OtpVerification(Base):
    """One row per OTP attempt. The actual OTP digits are never sent to or
    trusted from the client -- AgriDoot's genotp API returns otp_value to
    THIS backend, which stores a salted hash of it here and compares against
    what the user types in, server-side. See vyom/otp_client.py."""
    __tablename__ = "otp_verifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_cc = Column(String, nullable=False)
    phone = Column(String, nullable=False, index=True)
    # "signup" (new account, phone not yet in users table) or
    # "signin" (phone must already belong to an existing user)
    purpose = Column(String, nullable=False)

    # sha256(otp_value), never plaintext
    otp_hash = Column(String, nullable=False)
    # AgriDoot genotp's own otp_id, for support/debugging
    provider_otp_id = Column(String)
    provider_request_id = Column(String)  # AgriDoot genotp's own request_id

    attempts = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="5")
    verified = Column(Boolean, nullable=False, server_default="false")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
