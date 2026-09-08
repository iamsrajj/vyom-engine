"""
resanitize_farm_geometries.py -- one-off maintenance script.

Re-runs every stored farm's geometry through the new server-side sanitizer
(vyom/geometry_utils.py) and re-saves it if anything changed. This exists to
fix farms that were already saved with a degenerate near-duplicate vertex
BEFORE that sanitizer existed -- e.g. the farm(s) stuck in an infinite CDSE
400 Bad Request loop in tasks.refresh_farm, since a bad geometry never heals
itself; it fails the same way on every retry and every future poll_all_farms
sweep, forever, until the stored geometry itself is fixed.

Safe to re-run any time -- farms whose geometry is already clean are
untouched (no DB write, no refresh dispatched).

Usage:
    python -m scripts.resanitize_farm_geometries          # fix + refresh
    python -m scripts.resanitize_farm_geometries --dry-run  # report only
"""
import argparse
import logging

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import mapping, shape

from vyom.db import SessionLocal
from vyom.geometry_utils import sanitize_polygon_geojson
from vyom.models import Polygon
from vyom.tasks import refresh_farm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("resanitize_farm_geometries")


def main(dry_run: bool = False):
    db = SessionLocal()
    fixed, unchanged, failed = 0, 0, 0
    try:
        farms = db.query(Polygon).all()
        logger.info("Checking %d farm(s)...", len(farms))

        for farm in farms:
            original_geojson = mapping(to_shape(farm.geom))
            try:
                clean_geojson = sanitize_polygon_geojson(original_geojson)
            except ValueError as exc:
                failed += 1
                logger.error(
                    "Farm %s (%s): geometry unusable even after cleanup -- %s. "
                    "This one needs manual redraw, not just resanitizing.",
                    farm.id, farm.name, exc,
                )
                continue

            if clean_geojson == original_geojson:
                unchanged += 1
                continue

            fixed += 1
            logger.info("Farm %s (%s): geometry had degenerate vertices, cleaning.",
                        farm.id, farm.name)
            if dry_run:
                continue

            farm.geom = from_shape(shape(clean_geojson), srid=4326)
            db.add(farm)
            db.commit()

            # The old geometry's stored coverage/error state is stale for
            # this farm now -- re-run discovery so it actually gets data
            # instead of just silently having a clean geometry sitting
            # there until the next scheduled sweep (up to 6h away).
            refresh_farm.delay(str(farm.id), priority=True)
            logger.info("Farm %s: refresh re-dispatched.", farm.id)

    finally:
        db.close()

    logger.info(
        "Done. %d fixed%s, %d already clean, %d still unusable (need manual redraw).",
        fixed, " (dry-run, not saved)" if dry_run else "", unchanged, failed,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing to the DB or dispatching refreshes.")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
