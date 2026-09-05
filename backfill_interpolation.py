"""
backfill_interpolation.py -- run ON THE SERVER (same venv as API/workers)
to apply the new exact-date cloudy-fill logic to all EXISTING data.

fill_gaps_for_polygon() re-derives everything from the DB's current real
zonal_stats state every time it runs (see its docstring / tasks.py's own
comment on this) -- it's not additive-only, so it's always safe to re-run
for a farm+platform it's already run for. This just calls it for every
farm x platform combination that has any real data at all.

Usage:
    python3 backfill_interpolation.py
"""
import logging
import uuid

from vyom.db import SessionLocal
from vyom.models import Polygon, ZonalStat, CatalogProduct
from vyom.interpolation import fill_gaps_for_polygon
from sqlalchemy import select

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill_interpolation")


def main():
    db = SessionLocal()
    try:
        # Every (polygon_id, platform) pair that actually has zonal_stats
        # data -- no point running gap-fill for a farm+platform with zero
        # real readings, fill_gaps_for_polygon() would just no-op on it.
        pairs = db.execute(
            select(ZonalStat.polygon_id, CatalogProduct.platform)
            .join(CatalogProduct, ZonalStat.product_id == CatalogProduct.id)
            .distinct()
        ).all()

        logger.info(
            "Running gap-fill for %d farm+platform pair(s)...", len(pairs))
        done, failed = 0, 0
        for polygon_id, platform in pairs:
            try:
                changed = fill_gaps_for_polygon(db, polygon_id, platform)
                done += 1
                if changed:
                    logger.info("  farm=%s platform=%s: %d row(s) filled/updated",
                                polygon_id, platform, changed)
            except Exception:
                failed += 1
                db.rollback()
                logger.exception(
                    "Gap-fill failed for farm=%s platform=%s", polygon_id, platform)

        logger.info("Done. %d succeeded, %d failed.", done, failed)
    finally:
        db.close()


if __name__ == "__main__":
    main()
