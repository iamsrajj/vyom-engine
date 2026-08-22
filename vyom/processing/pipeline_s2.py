"""
pipeline_s2 -- per-tile processing loop for Sentinel-2 L2A products, computing
every index configured in settings.s2_indices (NDVI, NDWI, NDMI, MSAVI2, NDRE,
SOC_VIS by default -- add more by implementing the formula in indices.py and
listing its name in .env, no code change needed here).

Only reads each band once even though multiple indices reuse it (e.g. NDVI and
MSAVI2 both need Red+NIR) -- bands are read up front, indices computed from the
shared arrays.
"""
import glob
import logging
import os
import shutil
import tempfile
import zipfile

import numpy as np
import rasterio
import rasterio.windows
from rasterio.warp import transform_bounds
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.models import CatalogProduct
from vyom.processing.cloud_mask import build_valid_pixel_mask, cloud_fraction
from vyom.processing import indices as idx
from vyom.processing.cog_writer import write_index_cog
from vyom.storage import storage
from vyom.tile_grid import farms_bounds_for_product
from vyom.error_log import log_error

logger = logging.getLogger("vyom.processing.pipeline_s2")

_BAND_GLOBS = {
    "B02": "*_B02_10m.jp2",   # Blue, 10m
    "B03": "*_B03_10m.jp2",   # Green, 10m
    "B04": "*_B04_10m.jp2",   # Red, 10m
    "B05": "*_B05_20m.jp2",   # Red-edge 1, 20m
    "B08": "*_B08_10m.jp2",   # NIR, 10m
    "B11": "*_B11_20m.jp2",   # SWIR1, 20m
    "SCL": "*_SCL_20m.jp2",   # Scene Classification, 20m
}

# Which bands each index needs -- used to only read what's actually required.
_INDEX_BAND_REQUIREMENTS = {
    "NDVI": {"B08", "B04"},
    "NDWI": {"B03", "B08"},
    "NDMI": {"B08", "B11"},
    "NDRE": {"B08", "B05"},
    "MSAVI2": {"B08", "B04"},
    "SOC_VIS": {"B02", "B03", "B04"},
}


def _find_band_path(safe_extract_dir: str, band: str) -> str:
    pattern = os.path.join(safe_extract_dir, "**", _BAND_GLOBS[band])
    matches = glob.glob(pattern, recursive=True)
    if not matches:
        raise FileNotFoundError(
            f"Could not locate band {band} under {safe_extract_dir}")
    return matches[0]


def _extract_safe_zip(local_zip_path: str) -> str:
    extract_dir = local_zip_path.rsplit(".zip", 1)[0]
    if not os.path.isdir(extract_dir):
        logger.info("Extracting %s", local_zip_path)
        with zipfile.ZipFile(local_zip_path) as zf:
            zf.extractall(os.path.dirname(local_zip_path))
    return extract_dir


def _read_band(path: str, bounds_wgs84=None, target_shape=None, target_transform=None, resampling=rasterio.enums.Resampling.bilinear):
    """Read band `path`, windowed to `bounds_wgs84` (minx, miny, maxx, maxy in
    EPSG:4326 -- the CRS farm polygons are stored in) when given, instead of
    the full scene. S2 tiles are stored in their own UTM zone (meters), so the
    WGS84 bounds are reprojected into each src's own CRS before windowing --
    this also means it works correctly even when 10m and 20m bands (different
    pixel grids, same CRS) are windowed to the same geographic area."""
    with rasterio.open(path) as src:
        window = None
        if bounds_wgs84 is not None:
            bounds_native = transform_bounds(
                "EPSG:4326", src.crs, *bounds_wgs84)
            window = rasterio.windows.from_bounds(
                *bounds_native, transform=src.transform)
            window = window.intersection(
                rasterio.windows.Window(0, 0, src.width, src.height))
            window = window.round_offsets().round_lengths()
            if window.width <= 0 or window.height <= 0:
                raise ValueError(
                    f"Farm bounds {bounds_wgs84} do not overlap raster {path}")
        if target_shape is None:
            data = src.read(1, window=window)
            transform = src.window_transform(
                window) if window is not None else src.transform
            return data, transform, src.crs
        data = src.read(1, window=window, out_shape=target_shape,
                        resampling=resampling)
        return data, target_transform, src.crs


def process_product(db: Session, product: CatalogProduct) -> CatalogProduct:
    """Run the full per-tile processing loop, computing every configured S2 index."""
    if product.status == "processed":
        logger.info("Product %s already processed, skipping",
                    product.product_name)
        return product
    if not product.raw_path:
        raise FileNotFoundError(f"Raw file missing for {product.product_name}")

    # Storage abstraction gives us a local path to read from (downloads from S3
    # to a temp file if needed; no-op if already on local disk).
    local_zip_path = storage.open_for_read(product.raw_path)
    work_dir = tempfile.mkdtemp(prefix="vyom_proc_")

    try:
        if local_zip_path.startswith("/vsis3/"):
            # rasterio can read the zip's *contents* via /vsizip//vsis3/... without
            # a full local download -- but simplest/most robust for now is to
            # download once to local temp before extracting.
            import boto3
            raise NotImplementedError(
                "Direct S3 zip processing not wired up in this slice -- "
                "download_manager currently keeps a local copy path in raw_path "
                "even under S3 storage for the SAFE-zip extraction step."
            )

        safe_dir = _extract_safe_zip(local_zip_path)

        # Window every band read to the farms actually linked to this product
        # instead of loading the full ~110km x 110km tile -- that's the fix for
        # the OOM/SIGKILL: a farm-scale AOI is a few hundred pixels, not the
        # ~11000x11000 a full tile reads as at 10m.
        farm_bounds = farms_bounds_for_product(db, product.id)
        if farm_bounds is None:
            raise RuntimeError(
                f"No farms linked to product {product.product_name} yet -- "
                "process_product should only run after link_farm_to_products."
            )

        needed_indices = [
            i for i in settings.s2_indices if i in _INDEX_BAND_REQUIREMENTS]
        needed_bands = set().union(
            *(_INDEX_BAND_REQUIREMENTS[i] for i in needed_indices)) | {"SCL"}

        band_arrays = {}
        ref_transform = None
        ref_crs = None
        ref_shape = None

        # Read 10m bands first to establish the reference grid, then resample
        # 20m bands (B05, B11, SCL) up to match.
        for band in ("B02", "B03", "B04", "B08"):
            if band in needed_bands:
                path = _find_band_path(safe_dir, band)
                arr, transform, crs = _read_band(
                    path, bounds_wgs84=farm_bounds)
                band_arrays[band] = arr
                if ref_transform is None:
                    ref_transform, ref_crs, ref_shape = transform, crs, arr.shape

        for band in ("B05", "B11"):
            if band in needed_bands:
                path = _find_band_path(safe_dir, band)
                arr, _, _ = _read_band(
                    path, bounds_wgs84=farm_bounds, target_shape=ref_shape, target_transform=ref_transform)
                band_arrays[band] = arr

        scl_path = _find_band_path(safe_dir, "SCL")
        scl, _, _ = _read_band(
            scl_path, bounds_wgs84=farm_bounds, target_shape=ref_shape, target_transform=ref_transform,
            resampling=rasterio.enums.Resampling.nearest,
        )
        valid_mask = build_valid_pixel_mask(scl)
        cloud_pct = cloud_fraction(scl)

        computed = {}
        if "NDVI" in needed_indices:
            computed["NDVI"] = idx.compute_ndvi(
                band_arrays["B08"], band_arrays["B04"])
        if "NDWI" in needed_indices:
            computed["NDWI"] = idx.compute_ndwi(
                band_arrays["B03"], band_arrays["B08"])
        if "NDMI" in needed_indices:
            computed["NDMI"] = idx.compute_ndmi(
                band_arrays["B08"], band_arrays["B11"])
        if "NDRE" in needed_indices:
            computed["NDRE"] = idx.compute_ndre(
                band_arrays["B08"], band_arrays["B05"])
        if "MSAVI2" in needed_indices:
            computed["MSAVI2"] = idx.compute_msavi2(
                band_arrays["B08"], band_arrays["B04"])
        if "SOC_VIS" in needed_indices:
            computed["SOC_VIS"] = idx.compute_soc_vis(
                band_arrays["B02"], band_arrays["B03"], band_arrays["B04"])

        processed_paths = {}
        for name, array in computed.items():
            masked = np.where(valid_mask, array, np.nan)
            local_out = os.path.join(
                work_dir, f"{product.product_name}_{name}.tif")
            write_index_cog(masked, ref_transform, ref_crs, local_out)
            stored_path = storage.save_processed(
                local_out, f"{product.product_name}_{name}.tif")
            processed_paths[name] = stored_path

        product.processed_indices = processed_paths
        product.status = "processed"
        product.error_message = None
        product.cloud_cover = round(cloud_pct * 100, 2)

        db.add(product)
        db.commit()
        db.refresh(product)

        logger.info("Processed %s -> %d indices written",
                    product.product_name, len(processed_paths))

        # Raw .SAFE.zip (and its extracted contents) are always re-fetchable
        # from Copernicus and are by far the biggest disk consumer -- delete
        # them now that every index has been written out. Deliberately doesn't
        # touch product.raw_path in the DB (kept as a historical record of
        # where it *was*); if this product ever needs reprocessing, resetting
        # its status back to "discovered" makes download_product() re-fetch it
        # regardless of what raw_path says, since it only checks status, not
        # whether the file still exists on disk.
        try:
            storage.delete(product.raw_path)
            shutil.rmtree(safe_dir, ignore_errors=True)
            logger.info("Cleaned up raw data for %s", product.product_name)
        except Exception:  # noqa: BLE001 -- cleanup failure shouldn't fail an otherwise-successful process
            logger.warning("Could not clean up raw data for %s",
                           product.product_name, exc_info=True)

        return product

    except Exception as exc:  # noqa: BLE001
        product.status = "failed"
        product.error_message = str(exc)[:1000]
        db.add(product)
        db.commit()
        logger.exception("S2 processing failed for %s", product.product_name)
        log_error("pipeline_s2", str(exc), platform="S2",
                  context={"product_id": str(product.id), "product_name": product.product_name})
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
