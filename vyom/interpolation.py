"""
interpolation -- fills a fixed-cadence grid (default every 6 days) of values
for a farm+metric, computed BETWEEN two real zonal_stats observations.

HARD BOUNDARY, deliberate and non-negotiable: this only INTERPOLATES (fills
a gap between two known real points) -- it never EXTRAPOLATES (projects
before the first or after the last real observation). Forecasting a value
for a date with no real data on either side is a fundamentally different,
much less defensible claim than estimating a value between two real
readings, and this module refuses to do it. If the most recent real
observation is 10 days old, the grid stops there -- it does not invent a
"today" value.

Every interpolated_stats row records exactly which two real zonal_stats
rows it was computed from (left_zonal_stat_id, right_zonal_stat_id), so
provenance is always traceable, never just a bare number.

Method is linear interpolation only for now (see method column) -- simplest
defensible choice for values between two known points. Anything fancier
(harmonic/seasonal fitting, Savitzky-Golay smoothing) is a real methodology
decision that changes what the number means and should be a deliberate
follow-up, not silently swapped in here.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from vyom.models import ZonalStat, InterpolatedStat, CatalogProduct

logger = logging.getLogger("vyom.interpolation")

DEFAULT_GRID_DAYS = 6
# If a real observation already exists within this many days of a grid point,
# skip inserting an interpolated point there -- avoids a near-duplicate value
# sitting right next to a real one for no benefit.
MIN_DISTANCE_FROM_REAL_DAYS = 1


def fill_gaps_for_polygon(db: Session, polygon_id, platform: str,
                          grid_days: int = DEFAULT_GRID_DAYS) -> int:
    """Fills interpolated_stats for every metric that has real zonal_stats
    data for this polygon+platform. Returns the number of new interpolated
    rows inserted (0 if nothing new to fill, e.g. not enough real data yet
    or the grid is already fully covered)."""
    metrics = db.execute(
        select(ZonalStat.metric)
        .join(CatalogProduct, ZonalStat.product_id == CatalogProduct.id)
        .where(CatalogProduct.platform == platform, ZonalStat.polygon_id == polygon_id)
        .distinct()
    ).scalars().all()

    total_inserted = 0
    for metric in metrics:
        total_inserted += _fill_gaps_for_metric(db,
                                                polygon_id, platform, metric, grid_days)
    return total_inserted


def _fill_gaps_for_metric(db: Session, polygon_id, platform: str, metric: str, grid_days: int) -> int:
    real_points = db.execute(
        select(ZonalStat.id, ZonalStat.acquisition_date, ZonalStat.value)
        .join(CatalogProduct, ZonalStat.product_id == CatalogProduct.id)
        .where(
            CatalogProduct.platform == platform,
            ZonalStat.polygon_id == polygon_id,
            ZonalStat.metric == metric,
            ZonalStat.value.isnot(None),
        )
        .order_by(ZonalStat.acquisition_date)
    ).all()

    if len(real_points) < 2:
        return 0  # can't interpolate with fewer than 2 real anchor points

    existing_dates = set(db.execute(
        select(InterpolatedStat.date)
        .where(InterpolatedStat.polygon_id == polygon_id, InterpolatedStat.metric == metric)
    ).scalars().all())
    # Defensive normalization: DB drivers/dialects can hand back naive
    # datetimes even for a timezone(True) column in edge cases (seen this
    # exact break in SQLite during testing) -- comparing an aware cursor
    # against a naive existing_dates entry would crash a scheduled fill job
    # over something that should never be fatal. Treat naive as UTC.
    existing_dates = {
        d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)
        for d in existing_dates
    }

    inserted = 0
    for left, right in zip(real_points, real_points[1:]):
        left_id, left_date, left_value = left
        right_id, right_date, right_value = right
        if left_date.tzinfo is None:
            left_date = left_date.replace(tzinfo=timezone.utc)
        if right_date.tzinfo is None:
            right_date = right_date.replace(tzinfo=timezone.utc)
        if left_value is None or right_value is None:
            continue

        gap_days = (right_date - left_date).days
        if gap_days <= grid_days:
            continue  # gap already smaller than the target cadence, nothing to fill

        cursor = left_date + timedelta(days=grid_days)
        while cursor < right_date:
            too_close_to_real = (
                (cursor - left_date) < timedelta(days=MIN_DISTANCE_FROM_REAL_DAYS)
                or (right_date - cursor) < timedelta(days=MIN_DISTANCE_FROM_REAL_DAYS)
            )
            already_filled = any(
                abs((cursor - d).total_seconds()) < 3600 for d in existing_dates)

            if not too_close_to_real and not already_filled:
                fraction = (cursor - left_date).total_seconds() / \
                    (right_date - left_date).total_seconds()
                interpolated_value = Decimal(str(left_value)) + (
                    Decimal(str(right_value)) - Decimal(str(left_value))
                ) * Decimal(str(fraction))

                row = InterpolatedStat(
                    polygon_id=polygon_id,
                    platform=platform,
                    metric=metric,
                    date=cursor,
                    value=interpolated_value,
                    method="linear",
                    left_zonal_stat_id=left_id,
                    right_zonal_stat_id=right_id,
                )
                db.add(row)
                try:
                    db.flush()
                    inserted += 1
                except IntegrityError:
                    db.rollback()  # a concurrent run already inserted this exact point -- fine, skip

            cursor += timedelta(days=grid_days)

    db.commit()
    return inserted
