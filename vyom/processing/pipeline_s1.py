"""
pipeline_s1 -- per-tile processing loop for Sentinel-1 GRD products (VV+VH dual
polarization), computing RVI and the VV/VH ratio.

IMPORTANT PRODUCTION CAVEAT: Sentinel-1 GRD products ship as uncalibrated digital
numbers (DN), not physical backscatter values. Turning DN into calibrated sigma
naught backscatter requires applying the per-product calibration LUT (shipped in
the SAFE product's annotation/calibration XML) plus, ideally, terrain correction
(range/DEM-based) to remove topography-driven brightness variation. This slice
reads the raw DN and computes the indices directly from it -- which is enough to
get the pipeline wired end-to-end and see relative change over time for a FLAT
field, but is not radiometrically correct for absolute comparison across
different incidence angles, terrain, or between different fields. Before this
goes in front of farmers as a trustworthy number, wire in calibration (the
`snappy`/ESA SNAP Python API or the `pyroSAR` package are the standard routes)
between `_read_band` and `compute_rvi`/`compute_vv_vh_ratio` below.
"""
import glob
import logging
import os
import shutil
import tempfile
import zipfile

import numpy as np
import rasterio
import rasterio.transform
import rasterio.windows
from rasterio.warp import reproject, transform_bounds, Resampling
from shapely.geometry import box
from geoalchemy2.shape import from_shape
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.models import CatalogProduct
from vyom.processing import sar_indices
from vyom.processing.cog_writer import write_index_cog
from vyom.error_log import log_error
from vyom.storage import storage
from vyom.tile_grid import farms_bounds_for_product

logger = logging.getLogger("vyom.processing.pipeline_s1")


def _find_measurement(safe_extract_dir: str, pol: str) -> str:
    """S1 GRD measurement GeoTIFFs are named like s1a-iw-grd-vv-...tiff /
    s1a-iw-grd-vh-...tiff under measurement/."""
    pattern = os.path.join(safe_extract_dir, "**", f"*-grd-{pol}-*.tiff")
    matches = glob.glob(pattern, recursive=True)
    if not matches:
        raise FileNotFoundError(
            f"Could not locate {pol.upper()} measurement under {safe_extract_dir}")
    return matches[0]


def _extract_safe_zip(local_zip_path: str) -> str:
    extract_dir = local_zip_path.rsplit(".zip", 1)[0]
    if not os.path.isdir(extract_dir):
        logger.info("Extracting %s", local_zip_path)
        with zipfile.ZipFile(local_zip_path) as zf:
            zf.extractall(os.path.dirname(local_zip_path))
    return extract_dir


def _window_from_bounds_any_orientation(transform, bounds_native, raster_width, raster_height):
    """Compute a read window for `bounds_native` against `transform`, without
    assuming a north-up, axis-aligned raster the way rasterio.windows.
    from_bounds() does internally (it raises "Bounds and transform are
    inconsistent" whenever row/col end up reversed from what it expects).
    Used only for the (rare, for S1) case where the raster already has a real
    CRS -- see _read_band_windowed."""
    minx, miny, maxx, maxy = bounds_native
    inv = ~transform
    corners = [(minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)]
    cols, rows = zip(*(inv * corner for corner in corners))
    window = rasterio.windows.Window(
        col_off=min(cols), row_off=min(rows),
        width=max(cols) - min(cols), height=max(rows) - min(rows),
    )
    window = window.intersection(
        rasterio.windows.Window(0, 0, raster_width, raster_height))
    return window.round_offsets().round_lengths()


# Approximate S1 IW GRD ground pixel spacing, in degrees at the equator
# (~10m). Only used to size the destination grid when reprojecting a
# GCP-only-georeferenced band -- not meant to be geodesically exact.
_S1_PIXEL_SIZE_DEG = 0.00009


def _read_band_windowed(path: str, bounds_wgs84, dst_crs: str = "EPSG:4326"):
    """Read band 1 of `path`, restricted to `bounds_wgs84` (minx, miny, maxx,
    maxy in EPSG:4326 -- the CRS farm polygons are stored in) instead of the
    full scene -- a full S1 GRD scene is tens of thousands of pixels per side,
    which is what was causing the SIGKILL/OOM even at --concurrency=1.

    Sentinel-1 GRD measurement GeoTIFFs are NOT map-projected -- they ship in
    ground-range radar geometry, geolocated only by a coarse grid of GCPs (tie
    points). For that (the normal) case, this reprojects directly off the GCPs
    onto a clean, axis-aligned EPSG:4326 grid sized to just the farm bounds,
    using GDAL's GCP-based warp transformer (rasterio.warp.reproject(...,
    gcps=...)) -- which only touches the source pixels needed for that small
    destination window, so it stays cheap despite reprojecting.

    This replaces an earlier version that fit a single affine through the GCPs
    (rasterio.transform.from_gcps) and read raw pixels against it: that
    affine isn't guaranteed to be north-up/axis-aligned (it depends on the
    product's ascending/descending pass and row order), and while windowed
    reads against it worked, exactextract (zonal stats, downstream) explicitly
    rejects rotated rasters -- "Rotated rasters are not supported." Reprojecting
    up front avoids that: the destination transform is built with
    rasterio.transform.from_bounds, which is always rectilinear.

    If the raster *does* have a real CRS (true for Sentinel-2, and would be
    true for any already-geocoded S1 product), that path is unaffected and
    just windows against the existing transform as before -- no reprojection
    needed since it's already a proper grid."""
    with rasterio.open(path) as src:
        if src.crs is not None:
            bounds_native = transform_bounds(
                "EPSG:4326", src.crs, *bounds_wgs84)
            window = _window_from_bounds_any_orientation(
                src.transform, bounds_native, src.width, src.height)
            if window.width <= 0 or window.height <= 0:
                raise ValueError(
                    f"Farm bounds {bounds_wgs84} do not overlap raster {path}")
            data = src.read(1, window=window).astype("float32")
            transform = rasterio.windows.transform(window, src.transform)
            return data, transform, src.crs

        gcps, gcps_crs = src.gcps
        if not gcps:
            raise ValueError(
                f"{src.name} has no CRS and no GCPs -- cannot georeference it")
        gcps_crs = gcps_crs or "EPSG:4326"

        minx, miny, maxx, maxy = bounds_wgs84
        width = max(1, int(round((maxx - minx) / _S1_PIXEL_SIZE_DEG)))
        height = max(1, int(round((maxy - miny) / _S1_PIXEL_SIZE_DEG)))
        dst_transform = rasterio.transform.from_bounds(
            minx, miny, maxx, maxy, width, height)

        dst = np.full((height, width), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_crs=gcps_crs,
            gcps=gcps,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.bilinear,
            src_nodata=0,
            dst_nodata=np.nan,
        )
        return dst, dst_transform, dst_crs


def process_product(db: Session, product: CatalogProduct) -> CatalogProduct:
    if product.status == "processed":
        logger.info("Product %s already processed, skipping",
                    product.product_name)
        return product
    if not product.raw_path:
        raise FileNotFoundError(f"Raw file missing for {product.product_name}")

    work_dir = tempfile.mkdtemp(prefix="vyom_proc_s1_")

    try:
        # Same fix as pipeline_s2.py: get a genuine local file for zipfile
        # extraction rather than open_for_read's GDAL-only /vsis3/ virtual path.
        local_zip_path = storage.ensure_local_copy(product.raw_path, work_dir)

        safe_dir = _extract_safe_zip(local_zip_path)

        # Same fix as pipeline_s2: window to the farms actually linked to this
        # product instead of reading the full scene into memory. See
        # tile_grid.farms_bounds_for_product for why we use *all* linked farms,
        # not just the one that triggered this run.
        buffer_deg = float(
            product.cold_start_buffer_deg) if product.cold_start_buffer_deg is not None else 0.005
        farm_bounds = farms_bounds_for_product(
            db, product.id, buffer_deg=buffer_deg)
        if farm_bounds is None:
            raise RuntimeError(
                f"No farms linked to product {product.product_name} yet -- "
                "process_product should only run after link_farm_to_products."
            )

        vv_path = _find_measurement(safe_dir, "vv")
        vh_path = _find_measurement(safe_dir, "vh")

        vv, transform, crs = _read_band_windowed(vv_path, farm_bounds)
        vh, _, _ = _read_band_windowed(vh_path, farm_bounds)

        # Basic no-data / invalid pixel handling: GRD DN of 0 means no data.
        valid_mask = (vv > 0) & (vh > 0)
        vv = np.where(valid_mask, vv, np.nan)
        vh = np.where(valid_mask, vh, np.nan)

        computed = {}
        if "RVI" in settings.s1_indices:
            computed["RVI"] = sar_indices.compute_rvi(vv, vh)
        if "VV_VH_RATIO" in settings.s1_indices:
            computed["VV_VH_RATIO"] = sar_indices.compute_vv_vh_ratio(vv, vh)

        processed_paths = {}
        for name, array in computed.items():
            local_out = os.path.join(
                work_dir, f"{product.product_name}_{name}.tif")
            write_index_cog(array, transform, crs, local_out)
            stored_path = storage.save_processed(
                local_out, f"{product.product_name}_{name}.tif")
            processed_paths[name] = stored_path

        product.processed_indices = processed_paths
        # Same reasoning as pipeline_s2.py -- store the actual windowed
        # extent, reprojected to EPSG:4326 explicitly (not assumed, even
        # though _read_band_windowed's default dst_crs is already 4326 --
        # reprojecting explicitly here is a no-op if so, and stays correct
        # if that default ever changes).
        native_bounds = rasterio.transform.array_bounds(
            vv.shape[0], vv.shape[1], transform)
        wgs84_bounds = transform_bounds(crs, "EPSG:4326", *native_bounds)
        product.processed_bounds = from_shape(box(*wgs84_bounds), srid=4326)
        product.status = "processed"
        product.error_message = None

        db.add(product)
        db.commit()
        db.refresh(product)

        logger.info("Processed S1 %s -> %d indices written",
                    product.product_name, len(processed_paths))

        # Same rationale as pipeline_s2.py: raw .SAFE.zip is always
        # re-fetchable from Copernicus and is the biggest disk consumer --
        # delete it now that every index has been written out.
        try:
            storage.delete(product.raw_path)
            shutil.rmtree(safe_dir, ignore_errors=True)
            logger.info("Cleaned up raw data for %s", product.product_name)
        except Exception:  # noqa: BLE001
            logger.warning("Could not clean up raw data for %s",
                           product.product_name, exc_info=True)

        return product

    except Exception as exc:  # noqa: BLE001
        product.status = "failed"
        product.error_message = str(exc)[:1000]
        db.add(product)
        db.commit()
        logger.exception("S1 processing failed for %s", product.product_name)
        log_error("pipeline_s1", str(exc), platform="S1",
                  context={"product_id": str(product.id), "product_name": product.product_name})
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
