import logging
import time
import uuid
from datetime import date as date_cls, datetime, timezone
from math import isnan
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from geoalchemy2.shape import from_shape, to_shape
from pydantic import BaseModel, Field
from shapely.geometry import shape, mapping
from shapely.ops import transform as shapely_transform
import pyproj
from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.auth import require_auth, stable_owner_uuid
from vyom.db import get_db
from vyom.geometry_utils import sanitize_polygon_geojson
from vyom.models import Polygon, ZonalStat, CatalogProduct, InterpolatedStat, InterpolatedTile
from vyom.processing.index_scale import scales_for_api
from vyom.reuse_check import backfill_from_existing_products
from vyom.tasks import refresh_farm

logger = logging.getLogger("vyom.api.farms")

# Short-TTL in-process cache for the read-heavy per-farm/per-user data
# endpoints below (current-status, timeseries, latest, available-dates).
# Never used to skip an ownership check -- every endpoint below calls
# _get_owned_farm (or filters by owner directly) BEFORE touching this
# cache, so a cache hit can only ever return data the caller already
# proved they own.
_READ_CACHE_TTL_SECONDS = 6 * 3600
_read_cache: dict[str, tuple[float, object]] = {}


def _cached_read(key: str, compute):
    now = time.time()
    cached = _read_cache.get(key)
    if cached and now - cached[0] < _READ_CACHE_TTL_SECONDS:
        return cached[1]
    value = compute()
    _read_cache[key] = (now, value)
    return value


def _invalidate_farm_cache(farm_id, owner=None):
    """Drops every cached read for this farm, so the NEXT request re-queries
    Postgres instead of serving up to _READ_CACHE_TTL_SECONDS-old data.
    current-status isn't farm-scoped (one call returns every farm the owner
    has), so pass owner to also drop that owner's current-status entries.

    Call sites and what each does/doesn't guarantee:
      - farm_status(): fires the instant a farm's first real reading is
        seen, so the "fetching your field" overlay disappearing and the
        dashboard actually having data to show happen together, instead of
        the overlay vanishing into up to _READ_CACHE_TTL_SECONDS of a
        stale "no data" response.
      - refresh(): fires when a manual refresh is DISPATCHED, not when it
        completes (that happens later, async, in the Celery worker, which
        has no channel back into this cache). This only resets the cached
        snapshot to "accurate as of right now" -- it does NOT guarantee
        that whatever new data the refresh eventually pulls in will be
        visible before the next _READ_CACHE_TTL_SECONDS window elapses.
        If a manual refresh's results consistently need to show up sooner
        than that, this needs a real polling-based invalidation on the
        refresh path too (like farm_status() already has for onboarding),
        not just this dispatch-time clear."""
    farm_id_str = str(farm_id)
    stale_keys = [
        k for k in _read_cache
        if k.startswith(f"timeseries:{farm_id_str}:")
        or k.startswith(f"latest:{farm_id_str}:")
        or k.startswith(f"available-dates:{farm_id_str}:")
    ]
    if owner is not None:
        owner_str = str(owner)
        stale_keys += [k for k in _read_cache
                       if k.startswith(f"current-status:{owner_str}:")]
    for k in stale_keys:
        _read_cache.pop(k, None)


router = APIRouter(prefix="/farms", tags=["farms"])

_HA_TO_ACRE = 2.4710538147


def _to_utc(dt: datetime) -> datetime:
    """DB drivers can hand back a naive datetime for a timezone(True) column
    in edge cases -- treat naive as UTC so date-equality comparisons (used to
    match a cloudy ZonalStat row against its InterpolatedStat fill by exact
    date) don't silently fail to match, or crash, over a driver quirk."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _clean_float(value):
    """Some existing zonal_stats rows have NaN stored (from before this was
    sanitized at write time in zonal_stats.py) -- NaN isn't valid JSON, so it
    has to be swapped for None here too, or these routes 500 on any farm/date
    that hit that. Safe no-op for already-clean values."""
    if value is None:
        return None
    try:
        if isnan(value):
            return None
    except TypeError:
        return value
    return value


class FarmCreate(BaseModel):
    name: str
    # user_id REMOVED (security fix): this used to be a client-supplied field
    # trusted at face value for ownership, meaning any authenticated caller
    # could create a farm attributed to ANY other user's UUID just by putting
    # it in the request body. Ownership is now always derived server-side
    # from the authenticated session (see create_farm's use of
    # stable_owner_uuid(current_user)) -- never accepted from the client.
    geometry: dict = Field(...,
                           description="GeoJSON Polygon, any location worldwide")
    crop_type: Optional[str] = None
    soil_type: Optional[str] = None
    country: Optional[str] = None
    sowing_date: Optional[date_cls] = None
    # True when `geometry` is a rough placeholder (e.g. a small square around
    # a dropped map pin), not the farmer's final traced boundary. Used to
    # kick off the cold-start fetch (see refresh_farm/reuse_check) the
    # instant a rough location is known, running in parallel while the
    # farmer finishes drawing -- finalize with PATCH /farms/{id} once they
    # have the real polygon. Purely a lifecycle marker; fetch behavior is
    # identical either way.
    is_draft: bool = False


class FarmUpdate(BaseModel):
    """All fields optional -- only what's provided gets changed. Used mainly to
    set crop_type/sowing_date on a farm after it's already been created, or
    to finalize a draft farm's real geometry once the farmer finishes
    tracing it (see FarmCreate.is_draft) -- passing `geometry` here always
    clears is_draft and re-runs reuse-check + priority dispatch against the
    new shape, since it may now reach further than the rough placeholder
    did, or fall in different coverage entirely."""
    name: Optional[str] = None
    crop_type: Optional[str] = None
    soil_type: Optional[str] = None
    country: Optional[str] = None
    geometry: Optional[dict] = Field(
        None, description="GeoJSON Polygon -- the farmer's final traced boundary, replacing a draft's rough placeholder")
    sowing_date: Optional[date_cls] = None


class FarmOut(BaseModel):
    id: uuid.UUID
    name: Optional[str]
    user_id: uuid.UUID
    crop_type: Optional[str]
    soil_type: Optional[str]
    country: Optional[str]
    area_ha: Optional[float]
    area_acre: Optional[float]
    sowing_date: Optional[date_cls]
    crop_age_days: Optional[int]
    geometry: dict
    is_draft: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ZonalStatOut(BaseModel):
    acquisition_date: datetime
    metric: str
    value: Optional[float]
    cloud_pct: Optional[float]
    # "satellite" = a real zonal-stat computed from an actual acquired scene.
    # "interpolated" = a gap-filled estimate between TWO real observations
    # (see vyom/interpolation.py) -- never a forecast/extrapolation beyond
    # real data.
    # "provisional" = a flat carry-forward of the single most recent real
    # value, used only when no second real reading exists yet to properly
    # interpolate against. Weaker than "interpolated" -- gets deleted and
    # replaced with a real interpolated value the moment a new real reading
    # arrives. Every consumer of this field (dashboard, API clients,
    # reports) MUST preserve and display all three distinctly -- collapsing
    # them into indistinguishable "data" is exactly the misrepresentation
    # this field exists to prevent.
    source: str = "satellite"


def _geodesic_area_ha(geom_shape) -> float:
    """Proper equal-area calculation (not the flat lat/lon approximation) so
    area is accurate for farms anywhere in the world, not just near the equator."""
    project = pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:6933", always_xy=True).transform
    projected = shapely_transform(project, geom_shape)
    return projected.area / 10_000


def _to_farm_out(farm: Polygon) -> FarmOut:
    area_ha = float(farm.area_ha) if farm.area_ha is not None else None
    crop_age_days = (date_cls.today() -
                     farm.sowing_date).days if farm.sowing_date else None
    return FarmOut(
        id=farm.id,
        name=farm.name,
        user_id=farm.user_id,
        crop_type=farm.crop_type,
        soil_type=farm.soil_type,
        country=farm.country,
        area_ha=area_ha,
        area_acre=round(area_ha * _HA_TO_ACRE,
                        3) if area_ha is not None else None,
        sowing_date=farm.sowing_date,
        crop_age_days=crop_age_days,
        geometry=mapping(to_shape(farm.geom)),
        is_draft=farm.is_draft,
        created_at=_to_utc(farm.created_at),
    )


def _get_owned_farm(db: Session, farm_id: uuid.UUID, current_user: str) -> Polygon:
    """Fetches a farm and enforces ownership -- the BOLA fix. Returns 404
    (not 403) whether the farm doesn't exist at all OR belongs to someone
    else, so a caller can't distinguish 'no such farm' from 'that's not
    yours' and enumerate valid farm IDs belonging to other users."""
    farm = db.get(Polygon, farm_id)
    if not farm or farm.user_id != stable_owner_uuid(current_user):
        raise HTTPException(404, "Farm not found")
    return farm


@router.get("/available-indices")
def available_indices(response: Response):
    """What indices this deployment computes, per platform -- drives the index
    picker in the web UI instead of hardcoding options client-side.
    Cache-Control: this only ever changes on a redeploy (settings.s2_indices/
    s1_indices are config, not DB state), so it's safe for the browser to
    skip the round-trip entirely for a while -- paired with the frontend's
    own localStorage cache (see loadAvailableIndices() in web/index.html)."""
    response.headers["Cache-Control"] = "private, max-age=21600"
    return {"S2": settings.s2_indices, "S1": settings.s1_indices}


@router.get("/index-scales")
def index_scales(response: Response):
    """Discrete color-band scale (Low/Med/High tiers, hex colors, thresholds)
    per index -- drives the map legend and lets the frontend color-code the
    Latest Readings cards to match the tile colors, instead of the two being
    styled independently. Indices with no entry here (currently VV_VH_RATIO)
    have no defined scale; the frontend should render those uncolored.
    Same Cache-Control rationale as available_indices() above."""
    response.headers["Cache-Control"] = "private, max-age=21600"
    return scales_for_api()


def _backfill_and_dispatch_refresh(db: Session, farm: Polygon, priority: bool = True) -> dict:
    """Shared by create_farm, update_farm's geometry-finalize step, and the
    prewarm tool (vyom/prewarm.py): reuse-check this farm's CURRENT geometry
    against existing coverage, then dispatch a refresh for genuinely current
    data, sized as a cold-start cluster window for any platform reuse-check
    found zero coverage for. Returns the backfilled counts dict, mainly for
    logging by the caller.

    priority=True (the default, used by create_farm/update_farm) means a
    real person is waiting on this right now. priority=False MUST be used
    for any non-urgent/bulk dispatch (prewarm seeds) -- background work must
    never compete with a real farmer's request for the priority queues'
    dedicated capacity (see deploy/vyom-celery-worker-priority.service).

    Always dispatches with settings.initial_fetch_days_back /
    initial_fetch_max_cloud_cover (1 year / 85% cloud cover by default) --
    this is the one-time wide historical backfill a farm's real geometry
    gets the moment it's known, no longer configurable from the frontend
    (see the removed Look back / Max cloud cover sliders in web/index.html).
    The standing background sweep (poll_all_farms) is unaffected -- it
    always calls refresh_farm with its own tighter 30-day defaults for
    ongoing incremental catch-up, not this wide backfill window."""
    try:
        backfilled = backfill_from_existing_products(db, farm)
        if any(backfilled.values()):
            logger.info("Farm %s backfilled instantly: %s",
                        farm.id, backfilled)
    except Exception:  # noqa: BLE001 -- reuse-check must never block farm creation/update
        logger.exception(
            "Reuse-check failed for farm %s, continuing without backfill", farm.id)
        backfilled = {}

    cold_start_platforms = [p for p, count in backfilled.items() if count == 0] \
        if backfilled else ["S2", "S1"]

    refresh_farm.delay(
        str(farm.id),
        None,
        settings.initial_fetch_days_back,
        settings.initial_fetch_max_cloud_cover,
        priority=priority,
        cold_start_platforms=cold_start_platforms,
    )
    return backfilled


@router.post("", response_model=FarmOut)
def create_farm(payload: FarmCreate, current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        clean_geometry = sanitize_polygon_geojson(payload.geometry)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid farm boundary: {exc}")
    geom_shape = shape(clean_geometry)
    area_ha = _geodesic_area_ha(geom_shape)

    farm = Polygon(
        name=payload.name,
        user_id=stable_owner_uuid(current_user),
        geom=from_shape(geom_shape, srid=4326),
        crop_type=payload.crop_type,
        soil_type=payload.soil_type,
        country=payload.country,
        sowing_date=payload.sowing_date,
        area_ha=area_ha,
        is_draft=payload.is_draft,
    )
    db.add(farm)
    db.commit()
    db.refresh(farm)

    # Reuse-check + priority fetch: if this farm lands inside an already-
    # processed window (common for farms near existing coverage -- same
    # village/cluster, or added after an earlier campaign nearby, or from a
    # draft pin's own earlier cold-start fetch -- see update_farm's
    # geometry-finalize branch below), backfill its historical time series
    # INSTANTLY from existing data, with zero CDSE calls, then dispatch a
    # priority refresh for genuinely current data. Never blocks/fails farm
    # creation itself -- see _backfill_and_dispatch_refresh's try/except.
    _backfill_and_dispatch_refresh(db, farm)

    return _to_farm_out(farm)


@router.patch("/{farm_id}", response_model=FarmOut)
def update_farm(farm_id: uuid.UUID, payload: FarmUpdate, current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    """Partial update -- mainly for setting crop_type/sowing_date on a farm
    that was created before those were filled in, or correcting them later.

    Passing `geometry` finalizes a draft's rough placeholder boundary (or
    corrects any farm's boundary) -- recomputes area_ha, clears is_draft,
    and re-runs reuse-check + priority dispatch against the NEW shape. This
    is what makes the parallel-fetch-during-drawing flow actually pay off:
    the draft's cold-start fetch (triggered back at POST /farms time) has
    likely been running the whole time the farmer was tracing, so by the
    time this finalize call lands, the real final boundary often already
    falls inside that same window and hits reuse-check's instant path
    rather than triggering a second fresh CDSE fetch."""
    farm = _get_owned_farm(db, farm_id, current_user)

    updates = payload.model_dump(exclude_unset=True, exclude={"geometry"})
    for field, value in updates.items():
        setattr(farm, field, value)

    if payload.geometry is not None:
        try:
            clean_geometry = sanitize_polygon_geojson(payload.geometry)
        except ValueError as exc:
            raise HTTPException(422, f"Invalid farm boundary: {exc}")
        geom_shape = shape(clean_geometry)
        farm.geom = from_shape(geom_shape, srid=4326)
        farm.area_ha = _geodesic_area_ha(geom_shape)
        farm.is_draft = False

    db.add(farm)
    db.commit()
    db.refresh(farm)

    if payload.geometry is not None:
        _backfill_and_dispatch_refresh(db, farm)

    return _to_farm_out(farm)


@router.get("", response_model=list[FarmOut])
def list_farms(include_drafts: bool = False, current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    """Always scoped to the authenticated caller's own farms (security fix:
    this used to take an arbitrary `user_id` query param with no check that
    it matched the caller, and returned EVERY user's farms when that param
    was omitted entirely). include_drafts=False (default) hides rough
    placeholder farms still being traced (see FarmCreate.is_draft) AND
    prewarm seeds (see is_prewarm_seed) -- neither is a real farm a user
    should see."""
    owner = stable_owner_uuid(current_user)
    stmt = select(Polygon).where(Polygon.user_id == owner)
    if not include_drafts:
        stmt = stmt.where(Polygon.is_draft == False, Polygon.is_prewarm_seed == False)  # noqa: E712
    farms = db.execute(stmt).scalars().all()
    return [_to_farm_out(f) for f in farms]


@router.get("/current-status")
def current_status(metric: str = "NDVI_mean", current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    """Bulk 'what should the map show right now' endpoint -- one call for
    every farm instead of N calls, since a map render needs all of them at
    once. Registered BEFORE the /{farm_id} route below -- FastAPI matches
    routes in registration order, and "/farms/current-status" would
    otherwise incorrectly match /{farm_id} with farm_id="current-status"
    and fail UUID parsing before ever reaching this function.

    Scoped to the authenticated caller's own farms only (security fix: this
    used to query every farm from every user with no ownership filter at
    all -- the same BOLA class as list_farms/get_farm/etc, see
    _get_owned_farm's docstring).

    For each farm, returns the most recent REAL reading for `metric` plus a
    freshness signal:

      - source="satellite" if that real reading is within one grid period
        (DEFAULT_GRID_DAYS, currently 6 days) old -- genuinely current.
      - source="provisional" if it's older than that -- still the real
        last-known value (provisional carry-forward never changes the
        number, see vyom/interpolation.py), but the map should show this
        visually distinct from fresh data so a farmer isn't misled into
        thinking today's canopy looks like a reading from over a week ago.
      - source="no_data" if this farm has no real reading for `metric` yet.

    Deliberately computed from real ZonalStat.acquisition_date age directly,
    NOT by checking whether an interpolated_stats row happens to exist --
    that would make the map's freshness indicator depend on whether a
    background job happened to have run yet, which is fragile. Age-based
    staleness is always correct regardless of job timing.

    Cached in-process for _READ_CACHE_TTL_SECONDS (see top of file) keyed by
    (owner, metric) -- a fresh satellite reading landing via the Celery
    worker can take up to that long to show up here."""
    from vyom.interpolation import DEFAULT_GRID_DAYS
    from datetime import timezone as tz

    owner = stable_owner_uuid(current_user)

    def _compute():
        farms = db.execute(select(Polygon).where(
            Polygon.user_id == owner)).scalars().all()
        now = datetime.now(tz.utc)
        out = []
        for farm in farms:
            stmt = (
                select(ZonalStat)
                .where(ZonalStat.polygon_id == farm.id, ZonalStat.metric == metric)
                .order_by(ZonalStat.acquisition_date.desc())
                .limit(1)
            )
            row = db.execute(stmt).scalar_one_or_none()
            if row is None or row.value is None:
                out.append({
                    "farm_id": str(farm.id), "value": None,
                    "real_acquisition_date": None, "days_since_reading": None,
                    "source": "no_data",
                })
                continue

            acq_date = row.acquisition_date
            if acq_date.tzinfo is None:
                acq_date = acq_date.replace(tzinfo=tz.utc)
            days_since = (now - acq_date).days

            out.append({
                "farm_id": str(farm.id),
                "value": _clean_float(float(row.value)),
                "real_acquisition_date": row.acquisition_date,
                "days_since_reading": days_since,
                "source": "satellite" if days_since <= DEFAULT_GRID_DAYS else "provisional",
            })
        return out

    return _cached_read(f"current-status:{owner}:{metric}", _compute)


@router.get("/{farm_id}", response_model=FarmOut)
def get_farm(farm_id: uuid.UUID, current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    farm = _get_owned_farm(db, farm_id, current_user)
    return _to_farm_out(farm)


@router.delete("/{farm_id}")
def delete_farm(farm_id: uuid.UUID, current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    farm = _get_owned_farm(db, farm_id, current_user)
    db.delete(farm)
    db.commit()
    return {"status": "deleted"}


class RefreshRequest(BaseModel):
    platforms: Optional[list[str]] = None
    days_back: int = 30
    max_cloud_cover: Optional[float] = None
    # Routes this refresh's stage tasks to the priority queues (see
    # tasks.py/celery_app.py) so it isn't stuck behind background sweep
    # work. Defaults to False -- a manual refresh call isn't automatically
    # assumed to be someone actively waiting; pass true explicitly when it
    # is (e.g. an onboarding flow, a "refresh now" button a user just clicked).
    priority: bool = False


@router.post("/{farm_id}/refresh")
def refresh(farm_id: uuid.UUID, payload: RefreshRequest = RefreshRequest(),
            current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    """Kick off discover -> download -> process -> zonal-stats for this farm's
    imagery. platforms defaults to both S2 and S1. days_back defaults to 30 --
    pass a larger value (e.g. 60 or 90) to backfill more history in one go, such
    as right after creating a farm, or when a platform's cloud/revisit limits
    mean 30 days doesn't turn up much (S2 during monsoon season, for example).
    max_cloud_cover (S2 only) defaults to None, meaning "use settings.
    default_max_cloud_cover" (40%) -- pass a higher value to get results back
    when the default is too strict for the season. priority=true jumps this
    refresh's work ahead of background sweeps (see RefreshRequest.priority
    above) -- only actually effective once a dedicated priority worker
    process is running, see deploy/vyom-celery-worker-priority.service.
    Runs async via Celery."""
    farm = _get_owned_farm(db, farm_id, current_user)

    if payload.days_back < 1 or payload.days_back > 365:
        raise HTTPException(422, "days_back must be between 1 and 365")
    if payload.max_cloud_cover is not None and not (0 <= payload.max_cloud_cover <= 100):
        raise HTTPException(422, "max_cloud_cover must be between 0 and 100")

    task = refresh_farm.delay(
        str(farm_id), payload.platforms, payload.days_back, payload.max_cloud_cover, payload.priority)
    _invalidate_farm_cache(farm_id, owner=farm.user_id)
    return {
        "farm_id": str(farm_id),
        "task_id": task.id,
        "status": "queued",
        "platforms": payload.platforms or ["S2", "S1"],
        "days_back": payload.days_back,
        "max_cloud_cover": payload.max_cloud_cover,
        "priority": payload.priority,
    }


@router.get("/{farm_id}/status")
def farm_status(farm_id: uuid.UUID, current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    """Whether this farm has any real satellite reading yet -- backs the
    dashboard's "we're fetching your field's data" overlay (see
    web/index.html), shown from farm creation until the first real reading
    lands, since discovery -> download -> process -> stats for a brand-new
    area can take up to ~12-18 hours (see tasks.refresh_farm).

    progress_pct is a simple elapsed-time estimate for the progress bar --
    NOT a real pipeline-stage progress percentage, since there's no cheap
    way to know from here how many products/stages actually remain. It
    approaches but never reaches 100 until `ready` genuinely flips true, so
    it never falsely claims completion.

    Invalidates this farm's cached reads (see _invalidate_farm_cache) the
    moment has_real_data is true -- with _READ_CACHE_TTL_SECONDS at 6h,
    without this the "fetching your field" overlay could disappear (this
    endpoint isn't itself cached) while /latest and /available-dates kept
    serving a stale "no data" response for up to 6 more hours. Runs on
    every poll once ready (the frontend stops polling right after seeing
    ready=true, so in practice this fires once, not repeatedly)."""
    farm = _get_owned_farm(db, farm_id, current_user)

    has_real_data = db.execute(
        select(ZonalStat.id)
        .where(ZonalStat.polygon_id == farm_id, ZonalStat.value.isnot(None))
        .limit(1)
    ).scalar_one_or_none() is not None

    if has_real_data:
        _invalidate_farm_cache(farm_id, owner=farm.user_id)

    created_at = _to_utc(farm.created_at)
    elapsed_hours = (datetime.now(timezone.utc) -
                     created_at).total_seconds() / 3600

    # 18h = the upper end of the "maximum 12-18hrs" estimate shown to the
    # farmer -- the bar approaches (never reaches) 95% over that window,
    # then holds there until `ready` actually flips true.
    progress_pct = 100 if has_real_data else min(
        95, round(elapsed_hours / 18 * 95))

    return {
        "farm_id": str(farm_id),
        "ready": has_real_data,
        "created_at": created_at,
        "elapsed_hours": round(elapsed_hours, 2),
        "eta_hours_min": 12,
        "eta_hours_max": 18,
        "progress_pct": progress_pct,
    }


@router.get("/{farm_id}/timeseries", response_model=list[ZonalStatOut])
def timeseries(
    farm_id: uuid.UUID, metric: str = "NDVI_mean",
    include_interpolated: bool = False,
    current_user: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Time series for one metric (e.g. NDVI_mean, RVI_mean, SOC_VIS_mean).

    Real points always come from zonal_stats (satellite readings). When
    include_interpolated=true, computed points from interpolated_stats
    (see vyom/interpolation.py) are merged in on a fixed cadence:
      - source="interpolated": a real reading exists on both sides of the
        gap, linear interpolation between them.
      - source="provisional": only the most recent real reading exists so
        far (flat carry-forward of it) -- gets superseded by a real
        "interpolated" value the moment a new real reading arrives.
    Real, interpolated, and provisional points are never returned
    indistinguishably; every point states which one it is.

    Cached in-process for _READ_CACHE_TTL_SECONDS keyed by (farm_id, metric,
    include_interpolated) -- computed AFTER the ownership check below, so a
    cache hit can never leak another user's farm data."""
    farm = _get_owned_farm(db, farm_id, current_user)

    def _compute():
        stmt = (
            select(ZonalStat)
            .where(ZonalStat.polygon_id == farm_id, ZonalStat.metric == metric)
            .order_by(ZonalStat.acquisition_date)
        )
        rows = db.execute(stmt).scalars().all()

        interp_by_date = {}
        if include_interpolated:
            interp_stmt = (
                select(InterpolatedStat)
                .where(InterpolatedStat.polygon_id == farm_id, InterpolatedStat.metric == metric)
                .order_by(InterpolatedStat.date)
            )
            interp_by_date = {
                _to_utc(r.date): r for r in db.execute(interp_stmt).scalars().all()
            }

        out = []
        for r in rows:
            # A cloud-masked real acquisition (row exists, value is None) gets
            # its exact-date fill from interp_by_date if one exists (see
            # interpolation.py's _fill_exact_cloudy_dates) instead of surfacing
            # as a dead null -- a pass that happened but was unreadable is filled
            # in exactly like a gap between passes is.
            fill = interp_by_date.pop(_to_utc(
                r.acquisition_date), None) if r.value is None else None
            out.append(ZonalStatOut(
                acquisition_date=r.acquisition_date,
                metric=r.metric,
                value=_clean_float(float(r.value)) if r.value is not None else (
                    _clean_float(
                        float(fill.value)) if fill is not None and fill.value is not None else None
                ),
                cloud_pct=_clean_float(
                    float(r.cloud_pct)) if r.cloud_pct is not None else None,
                source="satellite" if r.value is not None else (
                    fill.source if fill is not None else "satellite"),
            ))

        if include_interpolated:
            # Remaining interpolated/provisional points are the fixed-cadence
            # grid fills that don't correspond to any real acquisition date at
            # all -- those still get appended as their own points, same as before.
            out.extend(
                ZonalStatOut(
                    acquisition_date=r.date,
                    metric=r.metric,
                    value=_clean_float(
                        float(r.value)) if r.value is not None else None,
                    cloud_pct=None,  # interpolated/provisional points have no real cloud reading
                    source=r.source,  # "interpolated" or "provisional" -- read from the row, never hardcoded
                )
                for r in interp_by_date.values()
            )
            out.sort(key=lambda x: x.acquisition_date)

        return out

    return _cached_read(
        f"timeseries:{farm_id}:{metric}:{include_interpolated}", _compute)


@router.get("/{farm_id}/latest")
def latest_snapshot(farm_id: uuid.UUID, date: str = "latest",
                    include_interpolated: bool = False,
                    current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    """Reading for every index this deployment computes, for one specific
    acquisition date (or the most recent one if date='latest' / omitted) --
    what a farm dashboard's summary cards bind to. `date` should be an exact
    ISO acquisition_date string, the same value the date-picker/date-chips use
    (from /available-dates), so switching dates on the dashboard shows the
    reading for that date instead of always the latest.

    include_interpolated=true: when no real ZonalStat row matches the exact
    date+index (true for any interpolated/provisional date -- those never
    have a real satellite reading by definition), falls back to
    InterpolatedStat. Every index in the response carries its own `source`
    ("satellite"/"interpolated"/"provisional"/null when no data at all) --
    the UI MUST show this, not just the number, same requirement as
    /timeseries and the tile endpoint's date-source labeling. Without this
    flag, an interpolated/provisional date correctly returns null values for
    every index (the old, pre-interpolation behavior) -- 'latest' is never
    affected either way, since 'latest' always means the most recent REAL
    reading (see tiles.py's _resolve_raster docstring for the same rule
    applied to map tiles).

    Cached in-process for _READ_CACHE_TTL_SECONDS keyed by (farm_id, date,
    include_interpolated) -- computed AFTER the ownership check below."""
    farm = _get_owned_farm(db, farm_id, current_user)

    target_date = None
    if date and date != "latest":
        try:
            target_date = datetime.fromisoformat(date)
        except ValueError:
            raise HTTPException(
                422, "date must be an ISO timestamp or 'latest'")

    def _compute():
        all_indices = settings.s2_indices + settings.s1_indices
        out = {}
        for index_name in all_indices:
            metric = f"{index_name}_mean"
            stmt = select(ZonalStat).where(ZonalStat.polygon_id ==
                                           farm_id, ZonalStat.metric == metric)
            if target_date is not None:
                stmt = stmt.where(ZonalStat.acquisition_date == target_date)
            else:
                # "latest" means the most recent REAL reading -- skip cloud-masked
                # rows (value IS NULL) rather than returning whichever row simply
                # has the newest date even if that specific pass was unusable.
                stmt = stmt.where(ZonalStat.value.isnot(None))
            stmt = stmt.order_by(ZonalStat.acquisition_date.desc()).limit(1)
            row = db.execute(stmt).scalar_one_or_none()

            if row is not None and row.value is not None:
                out[index_name] = {
                    "value": _clean_float(float(row.value)),
                    "acquisition_date": row.acquisition_date,
                    "source": "satellite",
                }
            elif include_interpolated and target_date is not None:
                interp_stmt = select(InterpolatedStat).where(
                    InterpolatedStat.polygon_id == farm_id,
                    InterpolatedStat.metric == metric,
                    InterpolatedStat.date == target_date,
                )
                interp_row = db.execute(interp_stmt).scalar_one_or_none()
                if interp_row is not None:
                    out[index_name] = {
                        "value": _clean_float(float(interp_row.value)) if interp_row.value is not None else None,
                        "acquisition_date": interp_row.date,
                        "source": interp_row.source,  # "interpolated" or "provisional"
                    }
                else:
                    out[index_name] = {"value": None,
                                       "acquisition_date": None, "source": None}
            else:
                out[index_name] = {"value": None,
                                   "acquisition_date": None, "source": None}
        return out

    return _cached_read(
        f"latest:{farm_id}:{date}:{include_interpolated}", _compute)


@router.get("/{farm_id}/available-dates")
def available_dates(farm_id: uuid.UUID, platform: str = "S2", index: Optional[str] = None,
                    include_interpolated: bool = False,
                    current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    """Dates with processed imagery for this farm -- populates a date picker in
    the UI instead of only ever showing 'latest'.

    Pass `index` (e.g. NDVI) to only return dates where that index actually has
    a real (non-null) zonal-stat value for this farm -- a product can be
    status='processed' (its COG exists, the map tile renders) while zonal
    stats for a given index are still missing/null, e.g. a fully cloud-masked
    scene, or a prior run that hit the per-index failure this was designed to
    guard against (see zonal_stats.py). Without `index`, returns every
    processed date regardless of zonal-stat completeness (the old behavior).

    Pass include_interpolated=true to also get interpolated/provisional map
    dates (see raster_interpolation.py) -- each entry states its own
    source ("satellite"/"interpolated"/"provisional") so the UI can label the
    date picker itself. This is the intended way to know which kind of tile
    a given date will serve BEFORE requesting it -- the tile PNG response's
    X-Vyom-Data-Source header carries the same info, but most map libraries
    (including Google Maps' getTileUrl pattern) load tiles as <img> elements,
    which JS cannot read response headers from. Use this endpoint, not the
    header, to drive any UI labeling.

    Cached in-process for _READ_CACHE_TTL_SECONDS keyed by (farm_id,
    platform, index, include_interpolated) -- computed AFTER the ownership
    check below."""
    farm = _get_owned_farm(db, farm_id, current_user)

    def _compute():
        from vyom.models import PolygonTileMap
        stmt = (
            select(CatalogProduct.acquisition_date)
            .join(PolygonTileMap, PolygonTileMap.product_id == CatalogProduct.id)
            .where(
                PolygonTileMap.polygon_id == farm_id,
                CatalogProduct.status == "processed",
                CatalogProduct.platform == platform,
            )
        )
        if index:
            stmt = stmt.join(
                ZonalStat,
                (ZonalStat.product_id == CatalogProduct.id)
                & (ZonalStat.polygon_id == farm_id)
                & (ZonalStat.metric == f"{index}_mean"),
            ).where(ZonalStat.value.isnot(None))
        stmt = stmt.order_by(CatalogProduct.acquisition_date.desc())
        rows = db.execute(stmt).scalars().all()
        result = [{"date": d.isoformat(), "source": "satellite"} for d in rows]

        if include_interpolated and index:
            interp_stmt = (
                select(InterpolatedTile.date, InterpolatedTile.source)
                .where(
                    InterpolatedTile.polygon_id == farm_id,
                    InterpolatedTile.platform == platform,
                    InterpolatedTile.index_name == index,
                )
                .order_by(InterpolatedTile.date.desc())
            )
            for d, src in db.execute(interp_stmt).all():
                result.append({"date": d.isoformat(), "source": src})
            result.sort(key=lambda r: r["date"], reverse=True)

        return result

    return _cached_read(
        f"available-dates:{farm_id}:{platform}:{index}:{include_interpolated}", _compute)
