"""
reuse_check -- when a new farm is created, check whether existing, already-
processed products already cover it before ever touching CDSE. This is the
highest-leverage piece of the pan-India rollout latency plan: any farm that
lands within an already-processed window (very common once real coverage
exists nearby -- neighboring farms in the same village/cluster, farms added
after an earlier campaign in the same area) gets its historical time series
populated in seconds, not minutes, and costs zero CDSE requests.

Correctness note: this checks against CatalogProduct.processed_bounds (the
ACTUAL windowed extent that was processed), not `footprint` (the full
satellite scene footprint). A farm can fall inside a product's footprint
while sitting outside the narrower window that was actually read/written --
see processed_bounds' docstring in models.py. Older products processed
before this column existed have processed_bounds = NULL and are correctly
excluded (safe default: no claimed coverage, not a false claim of coverage).

Reuse-check only ever backfills what already exists -- it never replaces the
need for a normal fresh discovery/download pass to get current data going
forward. Call this synchronously at farm-creation time for the instant
backfill, and still enqueue the normal refresh_farm flow for freshness.
"""
import logging

from geoalchemy2.functions import ST_Contains
from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.models import CatalogProduct, Polygon, InterpolatedTile
from vyom.tile_grid import link_farm_to_products
from vyom.zonal_stats import compute_zonal_stats_for_farms
from vyom.error_log import log_error

logger = logging.getLogger("vyom.reuse_check")


def backfill_from_existing_products(db: Session, farm: Polygon, platforms=("S2", "S1")) -> dict:
    """For each platform, finds every already-processed product whose
    processed_bounds fully contains this farm, links the farm to it, and
    computes zonal stats immediately -- giving the farm however much real
    historical data already happens to exist nearby, with zero CDSE calls.

    Returns {"S2": <count>, "S1": <count>} -- number of historical products
    backfilled per platform, for the API response / logging."""
    result = {}
    for platform in platforms:
        result[platform] = _backfill_platform(db, farm, platform)
    return result


def _backfill_platform(db: Session, farm: Polygon, platform: str) -> int:
    stmt = (
        select(CatalogProduct)
        .where(
            CatalogProduct.platform == platform,
            CatalogProduct.status == "processed",
            CatalogProduct.processed_bounds.isnot(None),
            ST_Contains(CatalogProduct.processed_bounds, farm.geom),
        )
        .order_by(CatalogProduct.acquisition_date)
    )
    try:
        covering_products = db.execute(stmt).scalars().all()
    except Exception as exc:  # noqa: BLE001 -- a DB/spatial-index hiccup must not block farm creation
        logger.exception(
            "Reuse-check spatial query failed for farm %s (%s)", farm.id, platform)
        log_error("reuse_check", str(exc), platform=platform,
                  context={"farm_id": str(farm.id)})
        return 0

    if not covering_products:
        return 0

    link_farm_to_products(db, farm, covering_products)

    backfilled = 0
    for product in covering_products:
        try:
            compute_zonal_stats_for_farms(db, product, [farm])
            backfilled += 1
        except Exception as exc:  # noqa: BLE001 -- one bad historical product shouldn't block the rest
            logger.exception(
                "Reuse-check backfill failed for farm %s against product %s (%s), continuing",
                farm.id, product.product_name, platform,
            )
            log_error("reuse_check", str(exc), platform=platform,
                      context={"farm_id": str(farm.id), "product_id": str(product.id)})

    logger.info("Reuse-check backfilled %d/%d %s product(s) for farm %s instantly, no CDSE calls",
                backfilled, len(covering_products), platform, farm.id)
    return backfilled
