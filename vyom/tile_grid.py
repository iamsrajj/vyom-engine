"""
tile_grid — maps farm polygons to the Copernicus products (tiles) that cover them.

Phase 1 simplification: instead of maintaining a separate MGRS tile-geometry
reference table (doc section 5.5), we intersect farm polygons directly against
catalog_products.footprint, which is already indexed with GiST. This is
functionally identical for the tile-first design principle — a product is still
processed once and its stats are computed for every intersecting farm in one
pass — it's just one join away instead of two.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session
from geoalchemy2.functions import ST_Intersects
from geoalchemy2.shape import to_shape

from vyom.models import CatalogProduct, Polygon, PolygonTileMap


def link_farm_to_products(db: Session, farm: Polygon, products: list[CatalogProduct]) -> None:
    """Ensure polygon_tile_map rows exist for every product that actually
    intersects this farm's geometry (products list may include near-misses
    from the discovery bounding query)."""
    for product in products:
        intersects = db.execute(
            select(ST_Intersects(farm.geom, product.footprint))
        ).scalar()
        if not intersects:
            continue

        exists = db.execute(
            select(PolygonTileMap).where(
                PolygonTileMap.polygon_id == farm.id,
                PolygonTileMap.product_id == product.id,
            )
        ).scalar_one_or_none()

        if not exists:
            db.add(PolygonTileMap(polygon_id=farm.id, product_id=product.id))

    db.commit()


def farms_for_product(db: Session, product_id: uuid.UUID) -> list[Polygon]:
    """All farms that intersect a given processed product — used by the
    processing pipeline to compute zonal stats for every relevant farm in
    one pass over the raster."""
    stmt = (
        select(Polygon)
        .join(PolygonTileMap, PolygonTileMap.polygon_id == Polygon.id)
        .where(PolygonTileMap.product_id == product_id)
    )
    return list(db.execute(stmt).scalars().all())


def farms_bounds_for_product(db: Session, product_id: uuid.UUID, buffer_deg: float = 0.005):
    """Union bounding box (minx, miny, maxx, maxy), in EPSG:4326, of every farm
    currently linked to this product, padded by buffer_deg (~500m at the
    equator -- plenty for a farm-scale AOI, not meant to be geodesically exact).

    Used by the processing pipeline to window raster reads to just the area
    farms actually need, instead of loading a full Sentinel-1 scene or
    Sentinel-2 tile (tens of thousands of pixels per side) into memory for
    what's usually a few hundred pixels of actual farm. Returns None if no
    farms are linked yet (shouldn't happen mid-refresh, but callers should
    handle it rather than assume).

    Windowing to *all* currently-linked farms, not just the one that triggered
    this run, matters because a product can be shared by multiple farms
    (polygon_tile_map is many-to-many) -- narrowing the window to a single
    farm would leave any other farm sharing the same tile with a raster that
    doesn't cover it.
    """
    farms = farms_for_product(db, product_id)
    if not farms:
        return None
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for farm in farms:
        fminx, fminy, fmaxx, fmaxy = to_shape(farm.geom).bounds
        minx, miny = min(minx, fminx), min(miny, fminy)
        maxx, maxy = max(maxx, fmaxx), max(maxy, fmaxy)
    return (minx - buffer_deg, miny - buffer_deg, maxx + buffer_deg, maxy + buffer_deg)
