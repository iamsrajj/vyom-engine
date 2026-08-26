"""
raster_interpolation -- the pixel-level (map/COG) counterpart to
interpolation.py's per-farm scalar numbers. Same rules, same honesty
boundary: "interpolated" only ever fills strictly BETWEEN two real COGs for
this farm; "provisional" is a flat carry-forward past the last real COG,
capped and superseded the moment a new real COG arrives. See
InterpolatedTile's docstring in models.py for the storage-reuse detail that
makes provisional tiles nearly free (no new file, just a DB row pointing at
the existing real COG).

Scoped PER FARM (polygon), not per shared product-cluster, deliberately:
two real products covering the same farm can have different pixel grids if
the set of farms sharing that product's window differed when each was
processed (see tile_grid.py's farms_bounds_for_product union-bbox design).
Reading both onto a farm-specific target grid via rasterio's reproject
(anchored to the LEFT real COG's own grid) avoids assuming two arbitrary
real COGs are pixel-aligned, which they are not guaranteed to be.
"""
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from vyom.models import CatalogProduct, PolygonTileMap, InterpolatedTile
from vyom.processing.cog_writer import write_index_cog
from vyom.storage import storage
from vyom.error_log import log_error

logger = logging.getLogger("vyom.raster_interpolation")

DEFAULT_GRID_DAYS = 6
MIN_DISTANCE_FROM_REAL_DAYS = 1
MAX_PROVISIONAL_PERIODS = 10  # matches interpolation.py's scalar cap


def fill_raster_gaps_for_polygon(db: Session, polygon_id, platform: str,
                                 grid_days: int = DEFAULT_GRID_DAYS) -> int:
    """Fills interpolated_tiles for every index that has real processed COGs
    for this polygon+platform. Returns the number of new/changed rows."""
    real_products = db.execute(
        select(CatalogProduct)
        .join(PolygonTileMap, PolygonTileMap.product_id == CatalogProduct.id)
        .where(
            PolygonTileMap.polygon_id == polygon_id,
            CatalogProduct.platform == platform,
            CatalogProduct.status == "processed",
        )
        .order_by(CatalogProduct.acquisition_date)
    ).scalars().all()

    if len(real_products) == 0:
        return 0

    changed = 0

    # ---- True pixel interpolation between each consecutive real pair ----
    if len(real_products) >= 2:
        for left, right in zip(real_products, real_products[1:]):
            shared_indices = set(left.processed_indices or {}) & set(
                right.processed_indices or {})
            if not shared_indices:
                continue
            changed += _fill_gap_pair(db, polygon_id, platform,
                                      left, right, shared_indices, grid_days)

    # ---- Provisional trailing edge (past the last real product) ----
    latest = real_products[-1]
    if latest.processed_indices:
        changed += _provisional_fill_trailing_edge(
            db, polygon_id, platform, latest, set(latest.processed_indices), grid_days)

    return changed


def _fill_gap_pair(db: Session, polygon_id, platform: str, left: CatalogProduct,
                   right: CatalogProduct, shared_indices: set, grid_days: int) -> int:
    left_date = _normalize(left.acquisition_date)
    right_date = _normalize(right.acquisition_date)
    gap_days = (right_date - left_date).days
    if gap_days <= grid_days:
        return 0

    # This gap is now closable with real data -- delete any provisional rows
    # sitting in it first (they never owned a separate file, so this is just
    # a DB delete, no storage cleanup needed).
    db.execute(
        delete(InterpolatedTile).where(
            InterpolatedTile.polygon_id == polygon_id,
            InterpolatedTile.platform == platform,
            InterpolatedTile.index_name.in_(shared_indices),
            InterpolatedTile.source == "provisional",
            InterpolatedTile.date > left_date,
            InterpolatedTile.date < right_date,
        )
    )
    db.flush()

    grid_dates = []
    cursor = left_date + timedelta(days=grid_days)
    while cursor < right_date:
        too_close = ((cursor - left_date) < timedelta(days=MIN_DISTANCE_FROM_REAL_DAYS)
                     or (right_date - cursor) < timedelta(days=MIN_DISTANCE_FROM_REAL_DAYS))
        if not too_close:
            grid_dates.append(cursor)
        cursor += timedelta(days=grid_days)
    if not grid_dates:
        return 0

    changed = 0
    for index_name in shared_indices:
        existing_dates = {
            _normalize(d) for d in db.execute(
                select(InterpolatedTile.date).where(
                    InterpolatedTile.polygon_id == polygon_id,
                    InterpolatedTile.platform == platform,
                    InterpolatedTile.index_name == index_name,
                )
            ).scalars().all()
        }
        dates_to_fill = [d for d in grid_dates
                         if not any(abs((d - e).total_seconds()) < 3600 for e in existing_dates)]
        if not dates_to_fill:
            continue

        try:
            aligned_left, aligned_right, transform, crs = _read_aligned_pair(
                left.processed_indices[index_name], right.processed_indices[index_name])
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to read/align COGs for %s interpolation between %s and %s",
                             index_name, left.product_name, right.product_name)
            log_error("raster_interpolation", str(exc), platform=platform,
                      context={"polygon_id": str(polygon_id), "index": index_name,
                               "left_product": left.product_name, "right_product": right.product_name})
            continue

        for grid_date in dates_to_fill:
            fraction = (grid_date - left_date).total_seconds() / \
                (right_date - left_date).total_seconds()
            interpolated_array = aligned_left + \
                (aligned_right - aligned_left) * fraction

            key = f"{polygon_id}/interpolated/{platform}/{index_name}/{grid_date.strftime('%Y%m%dT%H%M%S')}.tif"
            with tempfile.TemporaryDirectory() as tmpdir:
                local_path = os.path.join(tmpdir, "out.tif")
                write_index_cog(interpolated_array, transform, crs, local_path)
                stored_path = storage.save_processed(local_path, key)

            row = InterpolatedTile(
                polygon_id=polygon_id, platform=platform, index_name=index_name,
                date=grid_date, source="interpolated", storage_path=stored_path,
                left_product_id=left.id, right_product_id=right.id,
            )
            db.add(row)
            try:
                db.flush()
                changed += 1
            except IntegrityError:
                db.rollback()  # concurrent run already inserted this exact point

    db.commit()
    return changed


def _provisional_fill_trailing_edge(db: Session, polygon_id, platform: str,
                                    anchor: CatalogProduct, indices: set, grid_days: int) -> int:
    """No new raster is written here -- a provisional tile just points at the
    anchor product's own existing COG, unchanged. See InterpolatedTile's
    docstring for why this is deliberately free of extra storage cost."""
    anchor_date = _normalize(anchor.acquisition_date)
    now = datetime.now(timezone.utc)

    changed = 0
    for index_name in indices:
        existing_dates = {
            _normalize(d) for d in db.execute(
                select(InterpolatedTile.date).where(
                    InterpolatedTile.polygon_id == polygon_id,
                    InterpolatedTile.platform == platform,
                    InterpolatedTile.index_name == index_name,
                )
            ).scalars().all()
        }

        cursor = anchor_date + timedelta(days=grid_days)
        periods = 0
        while cursor <= now and periods < MAX_PROVISIONAL_PERIODS:
            periods += 1
            already_filled = any(
                abs((cursor - d).total_seconds()) < 3600 for d in existing_dates)
            if not already_filled:
                row = InterpolatedTile(
                    polygon_id=polygon_id, platform=platform, index_name=index_name,
                    date=cursor, source="provisional",
                    # reuse, no new file
                    storage_path=anchor.processed_indices[index_name],
                    left_product_id=anchor.id, right_product_id=None,
                )
                db.add(row)
                try:
                    db.flush()
                    changed += 1
                except IntegrityError:
                    db.rollback()
            cursor += timedelta(days=grid_days)

        if periods >= MAX_PROVISIONAL_PERIODS and cursor <= now:
            log_error(
                "raster_interpolation",
                f"Real raster gap for index {index_name} has exceeded "
                f"{MAX_PROVISIONAL_PERIODS * grid_days} days with no new reading.",
                level="warning", platform=platform,
                context={"polygon_id": str(polygon_id), "index": index_name},
                include_traceback=False,
            )

    db.commit()
    return changed


def _read_aligned_pair(left_storage_path: str, right_storage_path: str):
    """Reads the left COG as-is (it already correctly covers this farm, by
    construction -- it's a real processed product linked via
    polygon_tile_map). Reprojects the right COG onto the LEFT's exact
    grid/transform/CRS/shape via rasterio, since two arbitrary real products
    are not guaranteed to share a pixel grid (see module docstring). Returns
    (left_array, right_array_aligned, transform, crs) ready for elementwise
    interpolation."""
    with rasterio.open(storage.open_for_read(left_storage_path)) as left_ds:
        left_array = left_ds.read(1).astype("float32")
        left_array = np.where(left_array == left_ds.nodata, np.nan, left_array)
        transform, crs, shape = left_ds.transform, left_ds.crs, left_ds.shape

    with rasterio.open(storage.open_for_read(right_storage_path)) as right_ds:
        right_array = np.full(shape, np.nan, dtype="float32")
        reproject(
            source=rasterio.band(right_ds, 1),
            destination=right_array,
            src_transform=right_ds.transform, src_crs=right_ds.crs,
            dst_transform=transform, dst_crs=crs,
            resampling=Resampling.bilinear,
            src_nodata=right_ds.nodata, dst_nodata=np.nan,
        )

    return left_array, right_array, transform, crs


def _normalize(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
