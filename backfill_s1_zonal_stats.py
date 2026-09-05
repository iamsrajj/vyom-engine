"""
backfill_s1_zonal_stats.py -- run ON THE SERVER (same venv as API/workers)
to re-extract S1 zonal stats after the ON CONFLICT constraint-name bug is
fixed in vyom/zonal_stats.py. Every S1 zonal-stat write attempted while
that bug was live failed silently (caught per-index, logged as an error,
never actually written) -- this redoes them now that the fix is deployed.

Usage:
    python3 backfill_s1_zonal_stats.py
"""
import logging

from vyom.db import SessionLocal
from vyom.models import CatalogProduct
from vyom.zonal_stats import compute_zonal_stats_for_product

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill_s1_zonal_stats")


def main():
    db = SessionLocal()
    try:
        products = (
            db.query(CatalogProduct)
            .filter(CatalogProduct.platform == "S1",
                    CatalogProduct.status == "processed")
            .all()
        )
        logger.info(
            "Re-extracting zonal stats for %d S1 product(s)...", len(products))
        for p in products:
            compute_zonal_stats_for_product(db, p)
        logger.info("Re-extracted %d S1 products", len(products))
    finally:
        db.close()


if __name__ == "__main__":
    main()
