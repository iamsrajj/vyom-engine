"""
tasks -- Celery tasks chaining discovery -> download -> process -> zonal stats,
for both Sentinel-2 and Sentinel-1, per farm.
"""
import logging
import uuid
from shapely.geometry import mapping
from geoalchemy2.shape import to_shape

from vyom.celery_app import celery_app
from vyom.db import SessionLocal
from vyom.models import Polygon
from vyom.discovery import discover_products_for_geometry
from vyom.download_manager import download_product
from vyom.processing.pipeline import process_product
from vyom.zonal_stats import compute_zonal_stats_for_product
from vyom.interpolation import fill_gaps_for_polygon
from vyom.raster_interpolation import fill_raster_gaps_for_polygon
from vyom.tile_grid import link_farm_to_products
from vyom.error_log import log_error

logger = logging.getLogger("vyom.tasks")


def _run_pipeline_for_platform(db, farm: Polygon, platform: str, days_back: int = 30, max_cloud_cover: float | None = None) -> int:
    geometry = mapping(to_shape(farm.geom))
    products = discover_products_for_geometry(
        db, geometry, platform=platform, days_back=days_back, max_cloud_cover=max_cloud_cover
    )
    link_farm_to_products(db, farm, products)

    processed_count = 0
    for product in products:
        db.refresh(product)
        try:
            # Retry products stuck in "failed" too, not just fresh "discovered"
            # ones -- otherwise a product that failed once (e.g. during the old
            # zipper.dataspace.copernicus.eu outage) never gets picked up again,
            # and refresh_farm silently no-ops on it forever.
            if product.status in ("discovered", "failed"):
                download_product(db, product)
            if product.status == "downloaded":
                process_product(db, product)
            if product.status == "processed":
                compute_zonal_stats_for_product(db, product)
                processed_count += 1
        except Exception as exc:  # noqa: BLE001 -- one bad product shouldn't kill the whole farm refresh
            logger.exception(
                "Pipeline step failed for %s product %s, continuing", platform, product.product_name)
            log_error("tasks.refresh_farm", str(exc), platform=platform,
                      context={"farm_id": str(farm.id), "product_id": str(product.id), "product_name": product.product_name})
            continue
    if processed_count > 0:
        try:
            # Refresh gap-fill AFTER real zonal stats for this refresh cycle
            # are all in -- runs per-farm, once per refresh, not per-product,
            # since it needs the whole real time series to interpolate
            # correctly (not just the one product just processed).
            fill_gaps_for_polygon(db, farm.id, platform)
        except Exception:  # noqa: BLE001 -- gap-filling failure must never break real data processing
            logger.exception(
                "Gap-fill interpolation failed for farm %s (%s), real data unaffected", farm.id, platform)
            log_error("tasks.refresh_farm", "Gap-fill interpolation failed", platform=platform,
                      context={"farm_id": str(farm.id)})
        try:
            fill_raster_gaps_for_polygon(db, farm.id, platform)
        except Exception:  # noqa: BLE001 -- same isolation as the scalar fill above
            logger.exception(
                "Raster gap-fill failed for farm %s (%s), real data unaffected", farm.id, platform)
            log_error("tasks.refresh_farm", "Raster gap-fill failed", platform=platform,
                      context={"farm_id": str(farm.id)})
    return processed_count


@celery_app.task(name="vyom.discovery.refresh_farm", bind=True, max_retries=3, default_retry_delay=60)
def refresh_farm(self, farm_id: str, platforms: list[str] | None = None, days_back: int = 30, max_cloud_cover: float | None = None):
    """Discover, download, process, and compute stats for the newest S2 and/or
    S1 imagery covering one farm. platforms defaults to both. days_back defaults
    to 30 (matches discover_products_for_geometry's own default) -- callers can
    pass a larger value (e.g. 60-90) to backfill more history on a farm at once,
    such as right after creating it, without changing the standing default used
    by poll_all_farms' routine daily sweep. max_cloud_cover (S2 only; ignored for
    S1, which has no cloud concept) defaults to None, meaning "use settings.
    default_max_cloud_cover" -- pass a higher value (e.g. 80) to get scenes back
    during monsoon season when the 40% default legitimately finds nothing. Safe
    to call repeatedly -- every step dedupes."""
    platforms = platforms or ["S2", "S1"]
    db = SessionLocal()
    try:
        farm = db.get(Polygon, uuid.UUID(farm_id))
        if farm is None:
            logger.error("Farm %s not found", farm_id)
            return {"farm_id": farm_id, "status": "not_found"}

        results = {}
        for platform in platforms:
            results[platform] = _run_pipeline_for_platform(
                db, farm, platform, days_back=days_back, max_cloud_cover=max_cloud_cover
            )

        return {"farm_id": farm_id, "processed_per_platform": results}

    except Exception as exc:  # noqa: BLE001
        logger.exception("refresh_farm failed for %s", farm_id)
        log_error("tasks.refresh_farm", str(exc), context={
                  "farm_id": farm_id, "retry": self.request.retries})
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="vyom.discovery.poll_all_farms")
def poll_all_farms():
    """Background sweep: enqueue a refresh for every registered farm."""
    db = SessionLocal()
    try:
        farm_ids = [str(f.id) for f in db.query(Polygon.id).all()]
    finally:
        db.close()

    for farm_id in farm_ids:
        refresh_farm.delay(farm_id)

    logger.info("Enqueued refresh for %d farm(s)", len(farm_ids))
    return {"farms_enqueued": len(farm_ids)}
