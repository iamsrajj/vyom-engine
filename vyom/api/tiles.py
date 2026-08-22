"""
tiles -- XYZ PNG tile endpoint, generalized to any computed index (S2 or S1),
each with an appropriate colormap so the visual reads correctly (e.g. NDWI
should look blue-toward-water, not red-toward-green like NDVI).
"""
import uuid
from datetime import datetime
from typing import Optional

import numpy
from fastapi import APIRouter, Depends, HTTPException, Response
from geoalchemy2.shape import to_shape
from rasterio.crs import CRS
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session
from rio_tiler.io import Reader
from rio_tiler.colormap import cmap as default_cmaps

from vyom.db import get_db
from vyom.models import CatalogProduct, Polygon, PolygonTileMap
from vyom.processing.index_scale import rio_tiler_intervals
from vyom.storage import storage

router = APIRouter(prefix="/tiles", tags=["tiles"])

# Each index gets a colormap and rescale range chosen for what the values mean,
# not just NDVI's red-yellow-green reused everywhere.
_INDEX_RENDER_CONFIG = {
    "NDVI":        {"colormap": "rdylgn",  "range": (-1, 1)},
    "NDWI":        {"colormap": "rdbu",    "range": (-1, 1)},
    "NDMI":        {"colormap": "rdbu",    "range": (-1, 1)},
    "NDRE":        {"colormap": "rdylgn",  "range": (-1, 1)},
    "MSAVI2":      {"colormap": "rdylgn",  "range": (-1, 1)},
    "SOC_VIS":     {"colormap": "bugn",    "range": (0, 1)},
    "RVI":         {"colormap": "viridis", "range": (0, 1)},
    "VV_VH_RATIO": {"colormap": "viridis", "range": (0, 10)},
}


def _latest_product_for_farm(db: Session, farm_id: uuid.UUID, date: Optional[str], platform: str) -> CatalogProduct:
    stmt = (
        select(CatalogProduct)
        .join(PolygonTileMap, PolygonTileMap.product_id == CatalogProduct.id)
        .where(
            PolygonTileMap.polygon_id == farm_id,
            CatalogProduct.status == "processed",
            CatalogProduct.platform == platform,
        )
    )
    if date and date != "latest":
        target_date = datetime.fromisoformat(date)
        stmt = stmt.where(CatalogProduct.acquisition_date == target_date)
    stmt = stmt.order_by(CatalogProduct.acquisition_date.desc()).limit(1)

    product = db.execute(stmt).scalars().first()
    if not product:
        raise HTTPException(
            404, "No processed imagery found for this farm/date/platform")
    return product


@router.get("/{farm_id}/{date}/{z}/{x}/{y}.png")
def index_tile(
    farm_id: uuid.UUID,
    date: str,
    z: int,
    x: int,
    y: int,
    index: str = "NDVI",
    platform: str = "S2",
    db: Session = Depends(get_db),
):
    """
    date: an ISO acquisition date, or 'latest'.
    index: any index name from GET /farms/available-indices (e.g. NDVI, NDMI,
    NDRE, MSAVI2, SOC_VIS, RVI, VV_VH_RATIO).
    platform: 'S2' or 'S1' -- must match which platform computed that index.
    """
    index = index.upper()
    if index not in _INDEX_RENDER_CONFIG:
        raise HTTPException(400, f"Unknown index '{index}'")

    product = _latest_product_for_farm(db, farm_id, date, platform)

    farm = db.get(Polygon, farm_id)
    if farm is None:
        raise HTTPException(404, "Farm not found")

    stored_path = (product.processed_indices or {}).get(index)
    if not stored_path:
        raise HTTPException(
            404, f"No {index} raster available for this farm/date")

    cog_path = storage.open_for_read(stored_path)
    cfg = _INDEX_RENDER_CONFIG[index]

    try:
        with Reader(cog_path) as reader:
            img = reader.tile(x, y, z)
    except Exception:
        raise HTTPException(404, "Tile out of bounds for this scene")

    # A product's COG covers the buffered bounding box of every farm sharing
    # that tile (see tile_grid.farms_bounds_for_product) -- not just this one
    # farm's actual polygon. Without this, the tile shows the whole shared
    # processing box (a big square well past the field edge) instead of just
    # the farm. get_coverage_array gives per-pixel overlap with the farm
    # polygon; anywhere that's 0 gets masked out (rendered transparent).
    farm_geojson = mapping(to_shape(farm.geom))
    coverage = img.get_coverage_array(
        farm_geojson, shape_crs=CRS.from_epsg(4326))
    img.array[:, coverage <= 0] = numpy.ma.masked

    # Discrete, labelled bands (index_scale.py) for indices that have a defined
    # scale -- matches the color reference used for the reading-card legend too,
    # instead of a generic continuous gradient. Falls back to the old continuous
    # colormap for anything without a defined scale (currently just VV_VH_RATIO).
    intervals = rio_tiler_intervals(index)
    if intervals is not None:
        content = img.render(img_format="PNG", colormap=intervals)
    else:
        img.rescale(in_range=(cfg["range"],))
        content = img.render(
            img_format="PNG", colormap=default_cmaps.get(cfg["colormap"]))

    return Response(content=content, media_type="image/png")
