"""
farm_lifecycle -- everything that decides whether a farm may cost us money, and
everything that gives that money back when a farm goes away.

Why this module exists (cost + abuse protection):
  * Size limits: nobody can draw "all of India" and make the pipeline window,
    download and render a whole satellite tile.
  * Draft limits: a draft is only a placeholder pin. At most
    `max_draft_farms_per_user` per user, each <= `max_draft_acres`, and stale
    ones are deleted automatically.
  * Fetch eligibility: only farms that are paid (or partner-API farms of an
    account in good standing) are polled / refreshed. Drafts, unpaid farms and
    expired plans are not.
  * Cleanup: deleting a farm used to remove DB rows only. COG files, interpolated
    COGs and leftover raw zips stayed in storage forever (and queued Celery jobs
    kept downloading for a farm that no longer existed).
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pyproj
from shapely.ops import transform as shapely_transform
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.models import (
    CatalogProduct, FarmPlan, InterpolatedTile, Polygon, PolygonTileMap, User,
)
from vyom.units import HA_TO_ACRE

logger = logging.getLogger("vyom.farm_lifecycle")

_TO_EQUAL_AREA = pyproj.Transformer.from_crs(
    "EPSG:4326", "EPSG:6933", always_xy=True).transform


# ---------------------------------------------------------------- limits

def area_acres(geom_shape) -> float:
    return shapely_transform(_TO_EQUAL_AREA, geom_shape).area / 10_000 * HA_TO_ACRE


def extent_error(geom_shape, *, draft: bool = False) -> str | None:
    """Returns a human-readable reason if this boundary is too big, else None.
    Checked on create/update for dashboard AND partner-API farms."""
    minx, miny, maxx, maxy = geom_shape.bounds
    span = max(maxx - minx, maxy - miny)
    if span > settings.max_farm_bbox_deg:
        return (f"This boundary spans about {span * 111:.0f} km. A single field "
                f"must fit inside {settings.max_farm_bbox_deg * 111:.0f} km.")
    acres = area_acres(geom_shape)
    limit = settings.max_draft_acres if draft else settings.max_farm_acres
    if acres > limit:
        kind = "A draft (placeholder) field" if draft else "A field"
        return f"{kind} can be at most {limit:g} acres; this one is {acres:,.1f} acres."
    return None


# ---------------------------------------------------------------- drafts

def _draft_stmt(owner=None):
    stmt = select(Polygon).where(Polygon.is_draft.is_(True),
                                 Polygon.is_prewarm_seed.is_(False))
    if owner is not None:
        stmt = stmt.where(Polygon.user_id == owner)
    return stmt


def count_user_drafts(db: Session, owner) -> int:
    return db.execute(
        select(func.count()).select_from(_draft_stmt(owner).subquery())).scalar_one()


def purge_stale_drafts(db: Session, owner=None, max_age_minutes: int | None = None) -> int:
    """Delete drafts older than the TTL (optionally for one user only)."""
    age = settings.draft_ttl_minutes if max_age_minutes is None else max_age_minutes
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=age)
    stale = db.execute(_draft_stmt(owner).where(
        Polygon.created_at <= cutoff)).scalars().all()
    for farm in stale:
        delete_farm_data(db, farm)
    return len(stale)


# ---------------------------------------------------------------- eligibility

def fetchable_farms_stmt(now: datetime | None = None):
    """SQL for 'farms we are allowed to spend satellite/processing money on'."""
    now = now or datetime.now(timezone.utc)
    active_plan_farms = select(FarmPlan.farm_id).where(
        FarmPlan.status == "active",
        or_(FarmPlan.expires_at.is_(None), FarmPlan.expires_at > now))
    good_api_owners = select(User.id).where(
        User.business_status == "active",
        or_(User.business_api_payment_status.is_(None),
            User.business_api_payment_status != "suspended"))
    return select(Polygon).where(
        Polygon.is_draft.is_(False),
        Polygon.is_prewarm_seed.is_(False),
        or_(Polygon.id.in_(active_plan_farms),
            (Polygon.created_via == "api") & Polygon.user_id.in_(good_api_owners)))


def is_fetchable(db: Session, farm: Polygon) -> bool:
    if farm.is_prewarm_seed:
        # admin-triggered one-off, never polled (see poll_all_farms)
        return True
    if farm.is_draft:
        return settings.draft_prefetch_enabled
    row = db.execute(fetchable_farms_stmt().where(
        Polygon.id == farm.id)).first()
    return row is not None


# ---------------------------------------------------------------- deletion

def _processed_paths(product: CatalogProduct) -> list[str]:
    return [p for p in (product.processed_indices or {}).values() if p]


def _delete_files(paths) -> int:
    from vyom.storage import storage  # lazy: keeps tests/imports light
    removed = 0
    for path in {p for p in paths if p}:
        try:
            storage.delete(path)
            removed += 1
        except Exception:  # noqa: BLE001 - storage cleanup must never break deletion
            logger.warning("Could not delete %s", path, exc_info=True)
    return removed


def delete_farm_data(db: Session, farm: Polygon) -> dict:
    """Delete a farm and the storage objects ONLY this farm owned.

    Interpolated COGs are per-farm files (key '<farm_id>/interpolated/...'), so
    they go now. Real product COGs are shared with neighbouring farms: they are
    purged later by purge_orphan_products() once NO farm links to them.
    Queued Celery jobs for this farm are cancelled by the stage guards in
    tasks.py (they no-op when the product has no linked farm any more)."""
    owned = db.execute(
        select(InterpolatedTile.storage_path).where(
            InterpolatedTile.polygon_id == farm.id,
            InterpolatedTile.source == "interpolated")).scalars().all()
    farm_id = str(farm.id)  # read before delete: the row is gone after commit
    db.delete(farm)
    db.commit()
    files = _delete_files(owned)
    return {"farm_id": farm_id, "interpolated_files_deleted": files}


def purge_orphan_products(db: Session, grace_days: int | None = None) -> dict:
    """Delete processed products no farm links to any more (after a grace
    period so a farmer who deletes and re-adds a field reuses the data)."""
    grace = settings.orphan_product_grace_days if grace_days is None else grace_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace)
    linked = select(PolygonTileMap.product_id)
    orphans = db.execute(
        select(CatalogProduct).where(
            CatalogProduct.id.notin_(linked),
            CatalogProduct.updated_at <= cutoff,
            CatalogProduct.status.in_(("processed", "failed", "discovered")))
        .limit(500)).scalars().all()

    files = 0
    for product in orphans:
        paths = _processed_paths(product)
        paths += db.execute(
            select(InterpolatedTile.storage_path).where(
                InterpolatedTile.source == "interpolated",
                or_(InterpolatedTile.left_product_id == product.id,
                    InterpolatedTile.right_product_id == product.id))).scalars().all()
        files += _delete_files(paths)
        if product.raw_path:
            from vyom.storage import storage
            try:
                storage.delete(product.raw_path)
            except Exception:  # noqa: BLE001
                logger.warning("raw cleanup failed for %s",
                               product.raw_path, exc_info=True)
        db.delete(product)
    db.commit()
    return {"products_deleted": len(orphans), "files_deleted": files}
