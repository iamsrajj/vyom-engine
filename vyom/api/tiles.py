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

from vyom.auth import require_auth_query, stable_owner_uuid
from vyom.db import get_db
from vyom.models import CatalogProduct, Polygon, PolygonTileMap, InterpolatedTile
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
    # Everything below is in settings.s2_indices (config.py) and gets computed
    # and written to processed_indices for every product (pipeline_s2.py) --
    # any of them missing here means tile requests 400 outright before
    # _resolve_raster runs, even though a real COG exists in storage (this bit
    # us for EVI/ARI1/LAI_PROXY/CAR_RE/NDREX already). Add an entry here for
    # every index enabled in S2_INDICES, or its map layer will silently break
    # even though Readings/timeseries still show numbers for it.
    #
    # Ranges/colormaps are first-pass defaults reusing only colormap names
    # already proven to work in this file (rdylgn/rdbu/bugn/viridis) --
    # derived from each formula's documented value range in indices.py, not
    # validated against real farm imagery. Several (BAI, AWEI_*, WI2015,
    # MSI) are close to unbounded in theory, so their ranges are rough
    # clipping windows, not a claim about the "true" data range -- revisit
    # once there's real tile output to eyeball for each.
    "EVI":              {"colormap": "rdylgn",  "range": (-1, 1)},
    "ARI1":             {"colormap": "viridis", "range": (0, 0.3)},
    "LAI_PROXY":        {"colormap": "bugn",    "range": (0, 6)},
    "CAR_RE":           {"colormap": "viridis", "range": (0, 5)},
    "NDREX":            {"colormap": "rdylgn",  "range": (-1, 1)},
    "NDRE_B7":          {"colormap": "rdylgn",  "range": (-1, 1)},
    "EVI2":             {"colormap": "rdylgn",  "range": (-1, 1)},
    "NIRV":             {"colormap": "rdylgn",  "range": (-1, 1)},
    "OSAVI":            {"colormap": "rdylgn",  "range": (-1, 1)},
    "VARI":             {"colormap": "rdylgn",  "range": (-1, 1)},
    "SAVI":             {"colormap": "rdylgn",  "range": (-1, 1)},
    "MSI":              {"colormap": "viridis", "range": (0, 3)},
    "NDBI":             {"colormap": "viridis", "range": (-1, 1)},
    "IBI":              {"colormap": "viridis", "range": (-1, 1)},
    "BSI":              {"colormap": "viridis", "range": (-1, 1)},
    "NBR":              {"colormap": "rdbu",    "range": (-1, 1)},
    "NBR2":             {"colormap": "rdbu",    "range": (-1, 1)},
    "BAI":              {"colormap": "viridis", "range": (0, 50)},
    "MNDWI":            {"colormap": "rdbu",    "range": (-1, 1)},
    "AWEI_SH":          {"colormap": "rdbu",    "range": (-2, 2)},
    "AWEI_NSH":         {"colormap": "rdbu",    "range": (-3, 3)},
    "WI2015":           {"colormap": "rdbu",    "range": (-1, 2)},
    "NDSI":             {"colormap": "rdbu",    "range": (-1, 1)},
    "SNOW_BRIGHTNESS":  {"colormap": "viridis", "range": (0, 1)},
    "GREEN_BLUE_RATIO": {"colormap": "viridis", "range": (0, 3)},
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


def _resolve_raster(db: Session, farm_id: uuid.UUID, date: Optional[str],
                    platform: str, index: str, include_interpolated: bool) -> tuple[str, str]:
    """Returns (storage_path, source_label). source_label is one of
    "satellite", "interpolated", "provisional" -- the caller must always
    surface this (see the X-Vyom-Data-Source response header below), never
    silently serve a computed tile as if it were a real one.

    'latest' always means the most recent REAL product, regardless of
    include_interpolated -- interpolated/provisional tiles are only ever
    served for an explicit ISO date, matching the same explicit-opt-in
    pattern as /farms/{id}/timeseries?include_interpolated=true."""
    if date and date != "latest":
        target_date = datetime.fromisoformat(date)
        real_stmt = (
            select(CatalogProduct)
            .join(PolygonTileMap, PolygonTileMap.product_id == CatalogProduct.id)
            .where(
                PolygonTileMap.polygon_id == farm_id,
                CatalogProduct.status == "processed",
                CatalogProduct.platform == platform,
                CatalogProduct.acquisition_date == target_date,
            )
        )
        real_product = db.execute(real_stmt).scalars().first()
        if real_product:
            stored_path = (real_product.processed_indices or {}).get(index)
            if stored_path:
                return stored_path, "satellite"

        if include_interpolated:
            interp_stmt = select(InterpolatedTile).where(
                InterpolatedTile.polygon_id == farm_id,
                InterpolatedTile.platform == platform,
                InterpolatedTile.index_name == index,
                InterpolatedTile.date == target_date,
            )
            interp_row = db.execute(interp_stmt).scalars().first()
            if interp_row:
                return interp_row.storage_path, interp_row.source

        raise HTTPException(
            404, f"No {index} raster available for this farm/date")

    product = _latest_product_for_farm(db, farm_id, date, platform)
    stored_path = (product.processed_indices or {}).get(index)
    if not stored_path:
        raise HTTPException(
            404, f"No {index} raster available for this farm/date")
    return stored_path, "satellite"


@router.get("/{farm_id}/{date}/{z}/{x}/{y}.png")
def index_tile(
    farm_id: uuid.UUID,
    date: str,
    z: int,
    x: int,
    y: int,
    index: str = "NDVI",
    platform: str = "S2",
    include_interpolated: bool = False,
    current_user: str = Depends(require_auth_query),
    db: Session = Depends(get_db),
):
    """
    date: an ISO acquisition date, or 'latest'.
    index: any index name from GET /farms/available-indices (e.g. NDVI, NDMI,
    NDRE, MSAVI2, SOC_VIS, RVI, VV_VH_RATIO).
    platform: 'S2' or 'S1' -- must match which platform computed that index.
    include_interpolated: when true and no real reading exists for the exact
    date requested, falls back to a computed tile from interpolated_tiles
    (see raster_interpolation.py) -- the response's X-Vyom-Data-Source header
    always says which kind of tile was actually served ("satellite",
    "interpolated", or "provisional"). Never assume a 200 response is a real
    satellite reading without checking this header.
    """
    index = index.upper()
    if index not in _INDEX_RENDER_CONFIG:
        raise HTTPException(400, f"Unknown index '{index}'")

    farm = db.get(Polygon, farm_id)
    # Security fix (BOLA): this used to return any farm's tiles to any
    # authenticated caller with no ownership check at all -- same class of
    # bug as farms.py's endpoints before that fix, see
    # _get_owned_farm's docstring there for the reasoning (404, not 403,
    # so a caller can't distinguish "no such farm" from "not yours").
    if farm is None or farm.user_id != stable_owner_uuid(current_user):
        raise HTTPException(404, "Farm not found")

    stored_path, source_label = _resolve_raster(
        db, farm_id, date, platform, index, include_interpolated)

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

    return Response(
        content=content, media_type="image/png",
        headers={"X-Vyom-Data-Source": source_label},
    )
