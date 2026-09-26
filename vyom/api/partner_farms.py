"""api/partner_farms -- the business partner API (points 2/3 of the
monetization spec): create, update, and fetch farm + satellite-indices
data, authenticated via API key/secret (vyom/api_auth.py) rather than the
dashboard session.

Deliberately reuses the SAME geometry-sanitization, area-calculation, and
backfill-dispatch logic the dashboard's own farm creation uses (imported
directly from vyom.api.farms) -- this is a second entry point into the
same pipeline, not a parallel reimplementation, so every hard-won fix
there (degenerate-geometry handling, reuse-check, etc.) applies here too
automatically.

Scope, per the confirmed pricing split: farms created here get
created_via='api' and feature_tier='indices_only' -- satellite indices
only, no weather/Gyan AI/advisory. Billing is monthly in arrears via
BusinessApiInvoice (vyom/billing_tasks.py), NOT per-farm at creation --
these endpoints never touch Razorpay directly.
"""
import uuid
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Generic, Optional, TypeVar

from fastapi import APIRouter, Depends
from geoalchemy2.shape import from_shape, to_shape
from pydantic import BaseModel
from shapely.geometry import mapping, shape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vyom.api.farms import _backfill_and_dispatch_refresh, _geodesic_area_ha
from vyom.api_auth import ApiV1Error, BusinessApiContext, require_business_api_auth
from vyom.config import settings
from vyom.db import get_db
from vyom.geometry_utils import sanitize_polygon_geojson
from vyom.idempotency import check_and_replay, store_result
from vyom.models import Polygon, ZonalStat
from vyom.units import HA_TO_ACRE

router = APIRouter(prefix="/api/v1/farms", tags=["partner-api"])

T = TypeVar("T")


class Meta(BaseModel):
    # 'active' | 'business_inactive' -- always 'active' if we got past the auth gate
    account_status: str
    api_payment_status: str      # 'current' | 'suspended'
    farms_created_this_cycle: int
    next_invoice_date: str
    warnings: list[str]


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta


def _get_owned_api_farm(db: Session, farm_id: uuid.UUID, ctx: BusinessApiContext) -> Polygon:
    """Ownership check for the partner API: a farm must belong to this
    business account AND have been created THROUGH the API. A business
    account's own dashboard-created farms are invisible here even though
    they share the same user_id -- exactly the confirmed split (dashboard
    farms never appear in the API, regardless of who owns them)."""
    farm = db.get(Polygon, farm_id)
    if not farm or farm.user_id != ctx.user.id or farm.created_via != "api":
        raise ApiV1Error(404, "NOT_FOUND", "Farm not found")
    return farm


def _next_invoice_date(today: date_cls) -> str:
    first_of_this_month = today.replace(day=1)
    next_month = (first_of_this_month.replace(
        day=28) + timedelta(days=4)).replace(day=1)
    return next_month.isoformat()


def _build_meta(db: Session, ctx: BusinessApiContext) -> Meta:
    month_start = date_cls.today().replace(day=1)
    count = db.execute(
        select(func.count()).select_from(Polygon).where(
            Polygon.user_id == ctx.user.id, Polygon.created_via == "api",
            Polygon.created_at >= month_start,
        )
    ).scalar_one()
    return Meta(
        account_status="active", api_payment_status=ctx.user.business_api_payment_status,
        farms_created_this_cycle=count, next_invoice_date=_next_invoice_date(
            date_cls.today()),
        warnings=ctx.warnings,
    )


class PartnerFarmIn(BaseModel):
    name: Optional[str] = None
    geometry: dict
    crop_type: Optional[str] = None
    soil_type: Optional[str] = None
    country: Optional[str] = None
    sowing_date: Optional[date_cls] = None


class PartnerFarmUpdate(BaseModel):
    """All fields optional -- only supplied ones are changed.

    Deliberately does NOT accept `geometry` -- redrawing a farm's boundary
    via the partner API is intentionally unsupported. A farm's area is
    what its billing (₹55/acre/year) is calculated against; allowing a
    silent boundary change through an unattended API integration is both
    a billing-integrity risk and a data-integrity one (it would re-trigger
    the reuse-check/backfill pipeline against a new shape without any of
    the geometry-review a human gets on the dashboard's draw-and-confirm
    flow). To change a farm's boundary, delete and recreate it, or use the
    dashboard.
    """
    name: Optional[str] = None
    crop_type: Optional[str] = None
    soil_type: Optional[str] = None
    country: Optional[str] = None
    sowing_date: Optional[date_cls] = None


class PartnerFarmOut(BaseModel):
    id: uuid.UUID
    name: Optional[str]
    crop_type: Optional[str]
    soil_type: Optional[str]
    country: Optional[str]
    area_ha: Optional[float]
    area_acre: Optional[float]
    sowing_date: Optional[date_cls]
    geometry: dict
    created_via: str
    feature_tier: str
    created_at: datetime


def _to_partner_farm_out(farm: Polygon) -> PartnerFarmOut:
    area_ha = float(farm.area_ha) if farm.area_ha is not None else None
    return PartnerFarmOut(
        id=farm.id, name=farm.name, crop_type=farm.crop_type, soil_type=farm.soil_type,
        country=farm.country, area_ha=area_ha,
        area_acre=round(area_ha * HA_TO_ACRE,
                        3) if area_ha is not None else None,
        sowing_date=farm.sowing_date, geometry=mapping(to_shape(farm.geom)),
        created_via=farm.created_via, feature_tier=farm.feature_tier, created_at=farm.created_at,
    )


@router.post("", response_model=Envelope[PartnerFarmOut])
def create_partner_farm(
    payload: PartnerFarmIn,
    idempotency_key: Optional[str] = None,
    ctx: BusinessApiContext = Depends(require_business_api_auth),
    db: Session = Depends(get_db),
):
    body_for_hash = payload.model_dump(mode="json")
    replay = check_and_replay(db, credential_id=ctx.credential.id,
                              idempotency_key=idempotency_key, body=body_for_hash)
    if replay is not None:
        status_code, cached_body = replay
        return cached_body

    try:
        clean_geometry = sanitize_polygon_geojson(payload.geometry)
    except ValueError as exc:
        raise ApiV1Error(422, "VALIDATION_ERROR",
                         f"Invalid farm boundary: {exc}")

    geom_shape = shape(clean_geometry)
    area_ha = _geodesic_area_ha(geom_shape)

    farm = Polygon(
        name=payload.name, user_id=ctx.user.id, geom=from_shape(
            geom_shape, srid=4326),
        crop_type=payload.crop_type, soil_type=payload.soil_type, country=payload.country,
        sowing_date=payload.sowing_date, area_ha=area_ha, is_draft=False,
        created_via="api", feature_tier="indices_only",
    )
    db.add(farm)
    db.commit()
    db.refresh(farm)

    # Same reuse-check + priority-dispatch pipeline the dashboard uses --
    # never blocks/fails farm creation itself.
    _backfill_and_dispatch_refresh(db, farm)

    result = Envelope(data=_to_partner_farm_out(farm),
                      meta=_build_meta(db, ctx))
    result_dict = result.model_dump(mode="json")
    store_result(db, credential_id=ctx.credential.id, idempotency_key=idempotency_key,
                 body=body_for_hash, status_code=201, response_body=result_dict)
    return result


@router.patch("/{farm_id}", response_model=Envelope[PartnerFarmOut])
def update_partner_farm(
    farm_id: uuid.UUID, payload: PartnerFarmUpdate,
    idempotency_key: Optional[str] = None,
    ctx: BusinessApiContext = Depends(require_business_api_auth),
    db: Session = Depends(get_db),
):
    body_for_hash = {"farm_id": str(
        farm_id), **payload.model_dump(mode="json")}
    replay = check_and_replay(db, credential_id=ctx.credential.id,
                              idempotency_key=idempotency_key, body=body_for_hash)
    if replay is not None:
        _, cached_body = replay
        return cached_body

    farm = _get_owned_api_farm(db, farm_id, ctx)

    updates = payload.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        setattr(farm, field_name, value)

    db.add(farm)
    db.commit()
    db.refresh(farm)

    result = Envelope(data=_to_partner_farm_out(farm),
                      meta=_build_meta(db, ctx))
    result_dict = result.model_dump(mode="json")
    store_result(db, credential_id=ctx.credential.id, idempotency_key=idempotency_key,
                 body=body_for_hash, status_code=200, response_body=result_dict)
    return result


@router.get("", response_model=Envelope[list[PartnerFarmOut]])
def list_partner_farms(ctx: BusinessApiContext = Depends(require_business_api_auth),
                       db: Session = Depends(get_db)):
    farms = db.execute(
        select(Polygon).where(Polygon.user_id ==
                              ctx.user.id, Polygon.created_via == "api")
        .order_by(Polygon.created_at.desc()).limit(500)
    ).scalars().all()
    return Envelope(data=[_to_partner_farm_out(f) for f in farms], meta=_build_meta(db, ctx))


class SupportedIndicesOut(BaseModel):
    S2: list[str]
    S1: list[str]


@router.get("/indices", response_model=Envelope[SupportedIndicesOut])
def get_supported_indices(ctx: BusinessApiContext = Depends(require_business_api_auth),
                          db: Session = Depends(get_db)):
    """Every index this deployment computes, per satellite platform -- the
    partner-API equivalent of the dashboard's own GET /farms/available-indices
    (see vyom/api/farms.py). Same underlying config (settings.s2_indices/
    s1_indices), so this and the dashboard's index picker never drift apart.
    Registered before /{farm_id} below so "indices" is never swallowed as a
    farm_id path value.
    """
    return Envelope(
        data=SupportedIndicesOut(
            S2=settings.s2_indices, S1=settings.s1_indices),
        meta=_build_meta(db, ctx),
    )


@router.get("/{farm_id}", response_model=Envelope[PartnerFarmOut])
def get_partner_farm(farm_id: uuid.UUID, ctx: BusinessApiContext = Depends(require_business_api_auth),
                     db: Session = Depends(get_db)):
    farm = _get_owned_api_farm(db, farm_id, ctx)
    return Envelope(data=_to_partner_farm_out(farm), meta=_build_meta(db, ctx))


class IndexReading(BaseModel):
    metric: str
    acquisition_date: datetime
    value: Optional[float]
    cloud_pct: Optional[float]


class AvailableDate(BaseModel):
    date: date_cls


@router.get("/{farm_id}/available-dates", response_model=Envelope[list[AvailableDate]])
def get_partner_farm_available_dates(
    farm_id: uuid.UUID, metric: str = "NDVI_mean",
    ctx: BusinessApiContext = Depends(require_business_api_auth),
    db: Session = Depends(get_db),
):
    """Every date this farm has a real (non-null) satellite reading for the
    given metric, newest first -- pass one of these as `date` to
    /{farm_id}/indices, or use them to page through /{farm_id}/timeseries'
    full history. `metric` matches the same values /timeseries accepts
    (e.g. NDVI_mean, NDRE_mean) -- see /{farm_id}/indices' response for the
    full list this deployment computes for a given farm.
    """
    farm = _get_owned_api_farm(db, farm_id, ctx)
    rows = db.execute(
        select(ZonalStat.acquisition_date)
        .where(ZonalStat.polygon_id == farm.id, ZonalStat.metric == metric,
               ZonalStat.value.isnot(None))
        .order_by(ZonalStat.acquisition_date.desc())
    ).scalars().all()
    data = [AvailableDate(date=d.date() if isinstance(
        d, datetime) else d) for d in rows]
    return Envelope(data=data, meta=_build_meta(db, ctx))


@router.get("/{farm_id}/indices", response_model=Envelope[list[IndexReading]])
def get_partner_farm_latest_indices(
    farm_id: uuid.UUID, date: Optional[date_cls] = None,
    ctx: BusinessApiContext = Depends(require_business_api_auth),
    db: Session = Depends(get_db),
):
    """Every index this deployment computes, for this farm, on ONE
    acquisition date. Without `date`, returns each index's most recent
    REAL reading (no interpolated/provisional fallback -- kept simple for
    a v1 partner contract), same as before this parameter existed. With
    `date` (get one from /{farm_id}/available-dates), returns that exact
    date's readings instead -- an index with no reading on that date is
    simply omitted from the response, not returned as null, since unlike
    the dashboard's own /farms/{id}/latest this contract has no
    interpolated-fallback concept to fall back to.
    """
    farm = _get_owned_api_farm(db, farm_id, ctx)

    if date is not None:
        rows = db.execute(
            select(ZonalStat).where(
                ZonalStat.polygon_id == farm.id, ZonalStat.value.isnot(None),
                func.date(ZonalStat.acquisition_date) == date,
            )
        ).scalars().all()
    else:
        subq = (
            select(ZonalStat.metric, func.max(
                ZonalStat.acquisition_date).label("max_date"))
            .where(ZonalStat.polygon_id == farm.id, ZonalStat.value.isnot(None))
            .group_by(ZonalStat.metric)
            .subquery()
        )
        rows = db.execute(
            select(ZonalStat).join(
                subq, (ZonalStat.metric == subq.c.metric) &
                (ZonalStat.acquisition_date == subq.c.max_date))
            .where(ZonalStat.polygon_id == farm.id)
        ).scalars().all()

    data = [IndexReading(metric=r.metric, acquisition_date=r.acquisition_date,
                         value=float(r.value) if r.value is not None else None,
                         cloud_pct=float(r.cloud_pct) if r.cloud_pct is not None else None)
            for r in rows]
    return Envelope(data=data, meta=_build_meta(db, ctx))


@router.get("/{farm_id}/timeseries", response_model=Envelope[list[IndexReading]])
def get_partner_farm_timeseries(
    farm_id: uuid.UUID, metric: str = "NDVI_mean",
    start_date: Optional[date_cls] = None, end_date: Optional[date_cls] = None,
    ctx: BusinessApiContext = Depends(require_business_api_auth),
    db: Session = Depends(get_db),
):
    """Full history by default. Pass start_date and/or end_date (get real
    values from /{farm_id}/available-dates) to narrow to a specific window
    instead -- both bounds are inclusive."""
    farm = _get_owned_api_farm(db, farm_id, ctx)
    stmt = select(ZonalStat).where(ZonalStat.polygon_id ==
                                   farm.id, ZonalStat.metric == metric)
    if start_date is not None:
        stmt = stmt.where(func.date(ZonalStat.acquisition_date) >= start_date)
    if end_date is not None:
        stmt = stmt.where(func.date(ZonalStat.acquisition_date) <= end_date)
    rows = db.execute(stmt.order_by(
        ZonalStat.acquisition_date)).scalars().all()
    data = [IndexReading(metric=r.metric, acquisition_date=r.acquisition_date,
                         value=float(r.value) if r.value is not None else None,
                         cloud_pct=float(r.cloud_pct) if r.cloud_pct is not None else None)
            for r in rows]
    return Envelope(data=data, meta=_build_meta(db, ctx))
