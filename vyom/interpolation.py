"""
interpolation -- fills a fixed-cadence grid (default every 6 days) of values
for a farm+metric, using two distinct mechanisms depending on how much real
data is available. See InterpolatedStat's docstring in models.py for the
full explanation of the two `source` values this produces.

HARD BOUNDARY, deliberate and non-negotiable: nothing in this module ever
projects a TREND forward. The "provisional" mechanism repeats the single
most recent real value flat (carry-forward) -- it does not guess where the
value is heading. True interpolation only ever fills a gap strictly BETWEEN
two real observations, never before the first or after the last. The one
exception -- provisional carry-forward past the last real point, capped and
clearly labeled -- exists only because the user explicitly asked for a
"real data every 6 days" cadence even during a live gap, and it is
implemented so that:
  1. it never claims to be a real reading (source="provisional", distinct
     from both "satellite" and "interpolated" everywhere this is exposed)
  2. it is immediately superseded the moment real interpolation becomes
     possible (see the delete-then-reinsert step in _fill_gaps_for_metric)
  3. it stops extending after MAX_PROVISIONAL_PERIODS if the real gap runs
     unusually long, rather than silently repeating stale data forever

Every interpolated_stats row records which real zonal_stats row(s) it was
computed from, so provenance is always traceable, never just a bare number.
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from vyom.models import ZonalStat, InterpolatedStat, CatalogProduct
from vyom.error_log import log_error

logger = logging.getLogger("vyom.interpolation")

DEFAULT_GRID_DAYS = 6
# If a real observation already exists within this many days of a grid point,
# skip inserting a computed point there -- avoids a near-duplicate value
# sitting right next to a real one for no benefit.
MIN_DISTANCE_FROM_REAL_DAYS = 1
# Stop extending provisional carry-forward after this many grid periods with
# no new real data (10 periods * 6 days = 60 days) -- a live gap this long
# is itself worth surfacing as a real problem, not quietly papered over
# with an increasingly stale flat value.
MAX_PROVISIONAL_PERIODS = 10


def fill_gaps_for_polygon(db: Session, polygon_id, platform: str,
                          grid_days: int = DEFAULT_GRID_DAYS) -> int:
    """Fills interpolated_stats for every metric that has real zonal_stats
    data for this polygon+platform -- both true interpolation (between two
    real points) and provisional carry-forward (past the last real point,
    pending a new real reading). Returns the number of new/changed rows."""
    metrics = db.execute(
        select(ZonalStat.metric)
        .join(CatalogProduct, ZonalStat.product_id == CatalogProduct.id)
        .where(CatalogProduct.platform == platform, ZonalStat.polygon_id == polygon_id)
        .distinct()
    ).scalars().all()

    total_changed = 0
    for metric in metrics:
        total_changed += _fill_gaps_for_metric(db,
                                               polygon_id, platform, metric, grid_days)
    return total_changed


def _normalize(dt: datetime) -> datetime:
    """DB drivers/dialects can hand back naive datetimes even for a
    timezone(True) column in edge cases -- treat naive as UTC rather than
    let a comparison crash a scheduled fill job over a driver quirk."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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

    if len(real_points) == 0:
        return 0

    changed = 0

    # ---- True interpolation between each consecutive pair of real points ----
    if len(real_points) >= 2:
        for left, right in zip(real_points, real_points[1:]):
            left_id, left_date, left_value = left
            right_id, right_date, right_value = right
            left_date, right_date = _normalize(
                left_date), _normalize(right_date)
            if left_value is None or right_value is None:
                continue

            gap_days = (right_date - left_date).days
            if gap_days <= grid_days:
                continue  # already at/under target cadence, nothing to fill

            # This gap is now closable with real data -- any provisional
            # (carry-forward) rows sitting in it are stale placeholders from
            # before `right` existed. Delete them so they get replaced by
            # the real interpolated values below, never left lingering next
            # to a gap that's since become properly fillable.
            db.execute(
                delete(InterpolatedStat).where(
                    InterpolatedStat.polygon_id == polygon_id,
                    InterpolatedStat.metric == metric,
                    InterpolatedStat.source == "provisional",
                    InterpolatedStat.date > left_date,
                    InterpolatedStat.date < right_date,
                )
            )
            db.flush()

            existing_dates = {
                _normalize(d) for d in db.execute(
                    select(InterpolatedStat.date).where(
                        InterpolatedStat.polygon_id == polygon_id,
                        InterpolatedStat.metric == metric,
                    )
                ).scalars().all()
            }

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
                        polygon_id=polygon_id, platform=platform, metric=metric,
                        date=cursor, value=interpolated_value,
                        source="interpolated", method="linear",
                        left_zonal_stat_id=left_id, right_zonal_stat_id=right_id,
                    )
                    db.add(row)
                    try:
                        db.flush()
                        changed += 1
                    except IntegrityError:
                        db.rollback()  # concurrent run already inserted this exact point

                cursor += timedelta(days=grid_days)

    # ---- Provisional carry-forward past the LAST real point (trailing edge) ----
    latest_id, latest_date, latest_value = real_points[-1]
    latest_date = _normalize(latest_date)
    if latest_value is not None:
        changed += _provisional_fill_trailing_edge(
            db, polygon_id, platform, metric, grid_days,
            latest_id, latest_date, latest_value,
        )

    db.commit()
    return changed


def _provisional_fill_trailing_edge(db: Session, polygon_id, platform: str, metric: str,
                                    grid_days: int, anchor_id, anchor_date: datetime,
                                    anchor_value) -> int:
    """Flat carry-forward of the single most recent real value, on the same
    grid cadence, for every grid slot between the last real reading and now
    that doesn't yet have a real reading. Capped at MAX_PROVISIONAL_PERIODS
    so an unusually long real gap surfaces as a warning instead of silently
    repeating an increasingly stale value forever."""
    now = datetime.now(timezone.utc)
    existing_dates = {
        _normalize(d) for d in db.execute(
            select(InterpolatedStat.date).where(
                InterpolatedStat.polygon_id == polygon_id,
                InterpolatedStat.metric == metric,
            )
        ).scalars().all()
    }

    inserted = 0
    cursor = anchor_date + timedelta(days=grid_days)
    periods = 0
    while cursor <= now and periods < MAX_PROVISIONAL_PERIODS:
        periods += 1
        already_filled = any(abs((cursor - d).total_seconds())
                             < 3600 for d in existing_dates)
        if not already_filled:
            row = InterpolatedStat(
                polygon_id=polygon_id, platform=platform, metric=metric,
                date=cursor, value=Decimal(str(anchor_value)),
                source="provisional", method="carry_forward",
                left_zonal_stat_id=anchor_id, right_zonal_stat_id=None,
            )
            db.add(row)
            try:
                db.flush()
                inserted += 1
            except IntegrityError:
                db.rollback()
        cursor += timedelta(days=grid_days)

    if periods >= MAX_PROVISIONAL_PERIODS and cursor <= now:
        log_error(
            "interpolation",
            f"Real data gap for metric {metric} has exceeded "
            f"{MAX_PROVISIONAL_PERIODS * grid_days} days with no new reading -- "
            f"provisional carry-forward stopped extending further.",
            level="warning", platform=platform,
            context={"polygon_id": str(polygon_id), "metric": metric,
                     "last_real_date": anchor_date.isoformat()},
            include_traceback=False,
        )

    return inserted
