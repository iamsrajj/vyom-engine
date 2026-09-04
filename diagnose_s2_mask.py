"""
diagnose_s2_mask.py -- run this ON THE SERVER (same venv as the Celery
workers/API, so vyom.* imports resolve) to find out exactly why every S2
index is coming back null for a farm, while S1 works fine.

Usage:
    python diagnose_s2_mask.py <farm_id>

What it does, per recent S2 product linked to the farm:
  1. Re-reads the SCL band for the farm's window (same code path as
     pipeline_s2.py) and prints the SCL class histogram + how many
     pixels build_valid_pixel_mask() considers "good".
  2. Opens one of the already-written processed index COGs for that
     product and reports what fraction of pixels are nodata (-9999).

This tells us in one shot whether the bug is (a) the SCL read/mask itself
being empty even when real classes are present, or (b) the mask is fine
but something downstream (COG write, storage round-trip, exact_extract
read) is losing the data.
"""
import sys
import numpy as np
import rasterio

from vyom.database import SessionLocal
from vyom.models import CatalogProduct, PolygonTileMap, Polygon
from vyom.processing.pipeline_s2 import _read_band, _find_band_path, _extract_safe_zip
from vyom.processing.cloud_mask import build_valid_pixel_mask, cloud_fraction
from vyom.tile_grid import farms_bounds_for_product
from vyom.storage import storage


def main(farm_id: str):
    db = SessionLocal()
    farm = db.get(Polygon, farm_id)
    if farm is None:
        print(f"No farm with id {farm_id}")
        return

    products = (
        db.query(CatalogProduct)
        .join(PolygonTileMap, PolygonTileMap.product_id == CatalogProduct.id)
        .filter(
            PolygonTileMap.polygon_id == farm_id,
            CatalogProduct.platform == "S2",
            CatalogProduct.status == "processed",
        )
        .order_by(CatalogProduct.acquisition_date.desc())
        .limit(3)
        .all()
    )
    if not products:
        print("No processed S2 products linked to this farm at all.")
        return

    for product in products:
        print("=" * 70)
        print(f"Product: {product.product_name}  ({product.acquisition_date})")
        print(f"  stored cloud_cover: {product.cloud_cover}")
        print(
            f"  processed_indices keys: {list((product.processed_indices or {}).keys())[:5]}...")

        farm_bounds = farms_bounds_for_product(
            db, product.id, buffer_deg=0.005)
        print(f"  farm_bounds (wgs84): {farm_bounds}")

        # --- Part 1: does the raw SCL band actually contain good pixels? ---
        # We no longer have the raw SAFE.zip on disk (deleted after processing),
        # so this part only works if raw_path/extracted dir still exists. If it's
        # gone, skip straight to Part 2 -- that alone is diagnostic.
        try:
            local_zip = storage.ensure_local_copy(
                product.raw_path, "/tmp/diag_s2")
            import os
            if os.path.exists(local_zip):
                safe_dir = _extract_safe_zip(local_zip)
                scl_path = _find_band_path(safe_dir, "SCL")
                scl, _, _ = _read_band(scl_path, bounds_wgs84=farm_bounds)
                vals, counts = np.unique(scl, return_counts=True)
                print(f"  RAW SCL class histogram (bounds-windowed, native res): "
                      f"{dict(zip(vals.tolist(), counts.tolist()))}")
                mask = build_valid_pixel_mask(scl)
                print(f"  valid (good-class) pixels: {mask.sum()} / {mask.size} "
                      f"({100*mask.sum()/mask.size:.1f}%)")
            else:
                print(
                    "  raw SAFE.zip no longer on disk/Wasabi -- skipping raw SCL check")
        except Exception as e:
            print(
                f"  Could not re-read raw SCL ({e!r}) -- skipping raw SCL check")

        # --- Part 2: is the WRITTEN, already-processed COG actually empty? ---
        for idx_name, stored_path in (product.processed_indices or {}).items():
            try:
                raster_path = storage.open_for_read(stored_path)
                with rasterio.open(raster_path) as src:
                    data = src.read(1)
                    nodata = src.nodata
                    valid = data != nodata if nodata is not None else ~np.isnan(
                        data)
                    print(f"  COG[{idx_name}]: shape={data.shape} nodata={nodata} "
                          f"valid_pixels={valid.sum()}/{valid.size} "
                          f"({100*valid.sum()/valid.size:.1f}%) "
                          f"min/max where valid: "
                          f"{(data[valid].min(), data[valid].max()) if valid.any() else 'N/A'}")
            except Exception as e:
                print(f"  COG[{idx_name}]: FAILED TO OPEN -- {e!r}")
            break  # one index is enough to confirm the pattern; remove this line to check all

    db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python diagnose_s2_mask.py <farm_id>")
        sys.exit(1)
    main(sys.argv[1])
