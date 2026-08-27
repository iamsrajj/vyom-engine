"""
zonal_stats -- given a processed product (any platform), compute per-farm
mean/std for every index in product.processed_indices, in a single pass per
raster (doc's critical single-pass-per-tile performance pattern).
"""
import logging
import math

from exactextract import exact_extract
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy.orm import Session

from vyom.models import CatalogProduct, Polygon, ZonalStat
from vyom.tile_grid import farms_for_product
from vyom.storage import storage
from vyom.error_log import log_error

logger = logging.getLogger("vyom.zonal_stats")


def _clean_float(value):
    """exact_extract returns NaN (not None) for a farm window with zero valid
    pixels -- e.g. a scene that's fully cloud-masked over that specific farm.
    NaN is not valid JSON (Starlette's JSONResponse explicitly rejects it,
    raising "Out of range float values are not JSON compliant: nan"), so it
    has to become None before it ever reaches the database, or every read of
    it later 500s. count/pixel_count are already plain ints from exact_extract
    and don't need this."""
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except TypeError:
        pass
    return value


def _upsert_stat(db: Session, farm: Polygon, product: CatalogProduct, metric: str, value, pixel_count, cloud_pct):
    existing = (
        db.query(ZonalStat)
        .filter_by(polygon_id=farm.id, product_id=product.id, metric=metric)
        .one_or_none()
    )
    if existing:
        existing.value = value
        existing.pixel_count = pixel_count
        existing.cloud_pct = cloud_pct
    else:
        db.add(
            ZonalStat(
                polygon_id=farm.id,
                product_id=product.id,
                acquisition_date=product.acquisition_date,
                metric=metric,
                value=value,
                pixel_count=pixel_count,
                cloud_pct=cloud_pct,
            )
        )


def compute_zonal_stats_for_farms(db: Session, product: CatalogProduct, farms: list[Polygon]) -> int:
    """Core per-index, single-pass-per-raster logic, scoped to an explicit
    farms list rather than always 'every farm linked to this product'. Used
    directly by reuse_check.py's backfill path (compute stats for just ONE
    newly created farm against an already-processed historical product,
    without wastefully re-running exact_extract for every OTHER farm that
    already has stats for it). compute_zonal_stats_for_product() below is a
    thin wrapper over this for the normal 'just finished processing this
    product, update everyone it covers' case."""
    if not farms:
        return 0
    if product.status != "processed":
        raise ValueError(
            f"Product {product.product_name} is not processed yet (status={product.status})")

    farm_features = [
        {"type": "Feature", "geometry": mapping(to_shape(f.geom)), "properties": {
            "farm_id": str(f.id)}}
        for f in farms
    ]

    for index_name, stored_path in (product.processed_indices or {}).items():
        try:
            raster_path = storage.open_for_read(stored_path)
            results = exact_extract(raster_path, farm_features, [
                                    "mean", "stdev", "count"])

            for farm, result in zip(farms, results):
                props = result["properties"] if isinstance(
                    result, dict) and "properties" in result else result
                mean_val = _clean_float(props.get("mean"))
                std_val = _clean_float(props.get("stdev"))
                count_val = props.get("count")

                _upsert_stat(
                    db, farm, product, f"{index_name}_mean", mean_val, count_val, product.cloud_cover)
                _upsert_stat(
                    db, farm, product, f"{index_name}_std", std_val, count_val, product.cloud_cover)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Zonal stats failed for index %s on product %s, skipping just this index",
                index_name, product.product_name,
            )
            log_error("zonal_stats", f"Index {index_name} failed", platform=product.platform,
                      context={"product_id": str(product.id), "product_name": product.product_name, "index": index_name})

    db.commit()
    logger.info("Computed zonal stats for %d farm(s) against product %s", len(
        farms), product.product_name)
    return len(farms)


def compute_zonal_stats_for_product(db: Session, product: CatalogProduct) -> int:
    """For a processed product, compute {index}_mean/{index}_std for every farm
    intersecting it. Returns the number of farms updated."""
    farms = farms_for_product(db, product.id)
    if not farms:
        logger.info("No farms intersect product %s, nothing to do",
                    product.product_name)
        return 0
    return compute_zonal_stats_for_farms(db, product, farms)
