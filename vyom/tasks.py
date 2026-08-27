"""
tasks -- Celery tasks for discover -> download -> process -> zonal stats,
parallelized per-product across the dedicated download/process/stats queues
defined in celery_app.py.

WHY THIS FILE WAS RESTRUCTURED: previously, download/process/stats all ran
as plain Python function calls inside ONE task (refresh_farm), serially,
one product at a time. celery_app.py already defined separate download/
process/stats queues, but they were never actually used -- everything ran
wherever refresh_farm itself landed (the "discover" queue), so there was no
way to give downloads (CDSE-connection-limited, see cdse_rate_limiter.py)
different worker capacity than processing (CPU-bound), no way to run
multiple products in parallel, and no way to give a farm-onboarding request
priority over background sweep work, since it was all bundled into one
task type that ran start-to-finish before returning.

Each pipeline stage is now its own Celery task, routed to its own queue by
name prefix (see celery_app.py's task_routes). Every stage task is
idempotent and self-checking -- it re-fetches the product's CURRENT status
from the DB and no-ops if a prior stage hasn't actually succeeded yet. This
is what makes chaining them with `.si()` (immutable signatures, ignoring
the previous task's return value) both correct and simple: a chain just
calls each stage in order, and each stage decides for itself whether
there's real work to do, rather than trusting a return value that could be
stale by the time it actually runs on a different worker.
"""
import logging
import uuid

from celery import chain, chord, group
from shapely.geometry import mapping
from geoalchemy2.shape import to_shape

from vyom.celery_app import celery_app
from vyom.db import SessionLocal
from vyom.models import Polygon, CatalogProduct
from vyom.discovery import discover_products_for_geometry
from vyom.download_manager import download_product
from vyom.processing.pipeline import process_product
from vyom.zonal_stats import compute_zonal_stats_for_product
from vyom.interpolation import fill_gaps_for_polygon
from vyom.raster_interpolation import fill_raster_gaps_for_polygon
from vyom.tile_grid import link_farm_to_products
from vyom.error_log import log_error

logger = logging.getLogger("vyom.tasks")


# ============================================================
# Per-product pipeline stages
# ============================================================

@celery_app.task(name="vyom.download.download_product_task", bind=True,
                 max_retries=3, default_retry_delay=60)
def download_product_task(self, product_id: str) -> dict:
    db = SessionLocal()
    try:
        product = db.get(CatalogProduct, uuid.UUID(product_id))
        if product is None:
            return {"product_id": product_id, "status": "not_found"}
        # Idempotent no-op if this product is already past this stage (or
        # was handled by a concurrent dispatch) -- retry products stuck in
        # "failed" too, not just fresh "discovered" ones.
        if product.status not in ("discovered", "failed"):
            return {"product_id": product_id, "status": product.status}

        try:
            download_product(db, product)
            return {"product_id": product_id, "status": "downloaded"}
        except Exception as exc:  # noqa: BLE001
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc)
            # Retries exhausted -- return a failure status instead of raising
            # further, so the chain still "completes" from Celery's
            # perspective (the next stage's own status-check correctly
            # no-ops on a still-"failed" product) rather than the whole
            # chain/chord erroring out over one bad product.
            logger.exception(
                "download_product_task exhausted retries for %s", product_id)
            log_error("tasks.download", str(exc), context={
                      "product_id": product_id})
            return {"product_id": product_id, "status": "failed"}
    finally:
        db.close()


@celery_app.task(name="vyom.process.process_product_task", bind=True,
                 max_retries=2, default_retry_delay=30)
def process_product_task(self, product_id: str) -> dict:
    db = SessionLocal()
    try:
        product = db.get(CatalogProduct, uuid.UUID(product_id))
        if product is None:
            return {"product_id": product_id, "status": "not_found"}
        if product.status != "downloaded":
            return {"product_id": product_id, "status": product.status}

        try:
            process_product(db, product)
            return {"product_id": product_id, "status": "processed"}
        except Exception as exc:  # noqa: BLE001
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc)
            logger.exception(
                "process_product_task exhausted retries for %s", product_id)
            log_error("tasks.process", str(exc), context={
                      "product_id": product_id})
            return {"product_id": product_id, "status": "failed"}
    finally:
        db.close()


@celery_app.task(name="vyom.stats.compute_stats_task")
def compute_stats_task(product_id: str) -> dict:
    db = SessionLocal()
    try:
        product = db.get(CatalogProduct, uuid.UUID(product_id))
        if product is None:
            return {"product_id": product_id, "status": "not_found"}
        if product.status != "processed":
            return {"product_id": product_id, "status": product.status}

        try:
            compute_zonal_stats_for_product(db, product)
            return {"product_id": product_id, "status": "stats_done"}
        except Exception as exc:  # noqa: BLE001 -- per-index failures already
            # isolate inside compute_zonal_stats_for_product; this catches
            # anything else (e.g. a DB error) so the chord still fires cleanly.
            logger.exception("compute_stats_task failed for %s", product_id)
            log_error("tasks.stats", str(exc), context={
                      "product_id": product_id})
            return {"product_id": product_id, "status": "failed"}
    finally:
        db.close()


@celery_app.task(name="vyom.stats.fill_gaps_callback")
def fill_gaps_callback(results: list, farm_id: str, platform: str) -> dict:
    """Chord callback -- fires once every per-product chain dispatched for
    this farm+platform refresh has finished (success or handled failure --
    every stage task above returns a status dict rather than raising past
    its own retries, specifically so the chord always fires). `results` is
    the per-product status list Celery collects automatically; used here
    only for logging, since the gap-fill functions re-derive everything
    from the DB's current real state regardless of exactly which products
    succeeded this particular round."""
    succeeded = sum(1 for r in results if isinstance(
        r, dict) and r.get("status") == "stats_done")
    logger.info("Farm %s (%s): %d/%d product(s) completed this round, running gap-fill",
                farm_id, platform, succeeded, len(results))

    db = SessionLocal()
    try:
        if succeeded > 0:
            try:
                fill_gaps_for_polygon(db, uuid.UUID(farm_id), platform)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Gap-fill interpolation failed for farm %s (%s)", farm_id, platform)
                log_error("tasks.fill_gaps_callback", "Gap-fill interpolation failed",
                          platform=platform, context={"farm_id": farm_id})
            try:
                fill_raster_gaps_for_polygon(db, uuid.UUID(farm_id), platform)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Raster gap-fill failed for farm %s (%s)", farm_id, platform)
                log_error("tasks.fill_gaps_callback", "Raster gap-fill failed",
                          platform=platform, context={"farm_id": farm_id})
    finally:
        db.close()

    return {"farm_id": farm_id, "platform": platform, "succeeded": succeeded, "total": len(results)}


# ============================================================
# Entry points
# ============================================================

@celery_app.task(name="vyom.discovery.refresh_farm", bind=True, max_retries=3, default_retry_delay=60)
def refresh_farm(self, farm_id: str, platforms: list[str] | None = None,
                 days_back: int = 30, max_cloud_cover: float | None = None):
    """Discovers imagery for a farm and DISPATCHES the download/process/stats
    work as independent, parallel per-product chains across their own
    queues -- it does NOT run that work itself and does NOT block waiting
    for it to finish.

    REAL BEHAVIOR CHANGE from before: this task used to run the whole
    pipeline serially in-process and only return once everything was done.
    It now returns as soon as discovery + dispatch are complete (typically
    a few seconds), with the actual work continuing in the background. This
    was already safe for every existing caller: the /farms/{id}/refresh API
    endpoint always called this via .delay() and returned
    {"status": "queued"} immediately, never waiting on or inspecting this
    task's own return value -- confirmed before making this change, not
    assumed.

    Each discovered product gets its own chain (download -> process ->
    stats); every product's chain for a platform runs together in a group,
    so N products process in PARALLEL -- bounded by download/process worker
    concurrency, and for downloads specifically, by the CDSE connection-slot
    limiter (cdse_rate_limiter.py), not by this task looping one product at
    a time anymore. A chord callback (fill_gaps_callback) runs the
    interpolation gap-fill once all of a platform's product chains for this
    refresh have completed.

    days_back/max_cloud_cover behave exactly as before -- see prior
    docstring content preserved in git history if needed."""
    platforms = platforms or ["S2", "S1"]
    db = SessionLocal()
    try:
        farm = db.get(Polygon, uuid.UUID(farm_id))
        if farm is None:
            logger.error("Farm %s not found", farm_id)
            return {"farm_id": farm_id, "status": "not_found"}

        dispatched = {}
        for platform in platforms:
            geometry = mapping(to_shape(farm.geom))
            products = discover_products_for_geometry(
                db, geometry, platform=platform, days_back=days_back, max_cloud_cover=max_cloud_cover
            )
            # Both of these commit internally (discover_products_for_geometry
            # and link_farm_to_products) -- required, not optional, since the
            # chains dispatched below run in separate worker processes with
            # separate DB connections and would not see uncommitted rows.
            link_farm_to_products(db, farm, products)

            product_ids = [str(p.id) for p in products]
            if not product_ids:
                dispatched[platform] = {"products_found": 0, "chord_id": None}
                continue

            per_product_chains = [
                chain(
                    download_product_task.si(pid),
                    process_product_task.si(pid),
                    compute_stats_task.si(pid),
                )
                for pid in product_ids
            ]
            result = chord(group(per_product_chains))(
                fill_gaps_callback.s(farm_id, platform))
            dispatched[platform] = {
                "products_found": len(product_ids), "chord_id": result.id}

        return {"farm_id": farm_id, "dispatched": dispatched}

    except Exception as exc:  # noqa: BLE001 -- covers discovery-stage failures (e.g. CDSE unreachable)
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
