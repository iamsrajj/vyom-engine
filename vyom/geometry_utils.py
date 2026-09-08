"""
geometry_utils -- defensive cleanup for farmer-drawn polygons before they're
stored or ever sent to CDSE.

BACKGROUND: CDSE's OGC geometry validator rejects a ring that contains a
near-duplicate consecutive vertex pair (e.g. two accidental clicks ~1m apart
while tracing a boundary, or two vertices ~1m apart right before the ring
closes) as a degenerate/zero-length edge -- with nothing more helpful than a
bare "400 Bad Request" that gives no indication which vertex is at fault.
This has now happened twice in production from a client-side-only fix
(web/index.html's polygonToGeoJSON) that silently regressed/was lost between
deployments. This module is the server-side backstop: EVERY farm geometry is
sanitized here before it's stored, regardless of what the frontend does or
doesn't do, so this class of bug can't recur just because a client-side fix
gets lost again.
"""
import logging

import shapely
from shapely.geometry import shape, mapping, Polygon
from shapely.validation import make_valid

logger = logging.getLogger("vyom.geometry_utils")

# ~1m at the equator (111,320 m/degree) -- mirrors the tolerance the
# client-side dedup in web/index.html's polygonToGeoJSON is meant to use.
_SNAP_GRID_DEG = 0.00001


def has_defect(geometry: dict) -> bool:
    """True if this raw GeoJSON polygon actually has the defect
    sanitize_polygon_geojson exists to fix: a near-duplicate-but-not-exact
    consecutive vertex pair within ~1m of each other (anywhere in the ring,
    including the pair right before the ring's normal closing point), or
    general topological invalidity.

    Deliberately does NOT compare sanitize_polygon_geojson's output against
    the original -- shapely.set_precision() can renumber which vertex a
    ring starts at and reformat float precision even on an already-clean
    polygon, which would make a plain equality check misfire as "changed"
    on every single polygon, clean or not. This checks the raw input
    directly instead, so maintenance tooling only touches farms that
    actually need it."""
    poly = shape(geometry)
    if poly.geom_type != "Polygon":
        return True
    if not poly.is_valid:
        return True

    rings = [list(poly.exterior.coords)] + \
        [list(ring.coords) for ring in poly.interiors]
    for coords in rings:
        for i in range(len(coords) - 1):
            p1, p2 = coords[i], coords[i + 1]
            if p1 == p2:
                # exact duplicate (e.g. the ring's normal closing point) -- fine
                continue
            if abs(p1[0] - p2[0]) <= _SNAP_GRID_DEG and abs(p1[1] - p2[1]) <= _SNAP_GRID_DEG:
                return True
    return False


def sanitize_polygon_geojson(geometry: dict) -> dict:
    """Cleans a GeoJSON Polygon dict for storage/CDSE use:
      1. snaps coordinates to a ~1m grid, collapsing near-duplicate vertices
         (accidental double-clicks, floating point noise) into exact
         duplicates
      2. drops consecutive exact-duplicate vertices
      3. fixes minor self-intersections / degenerate rings via make_valid()
      4. re-closes the ring

    Raises ValueError if what's left after cleanup isn't a usable polygon
    (e.g. the farmer's points collapsed to a line once cleaned) -- callers
    should turn this into a 422, not let a bad geometry reach the DB/CDSE.
    """
    poly = shape(geometry)
    if poly.geom_type != "Polygon":
        raise ValueError(f"Expected a Polygon, got {poly.geom_type}")

    poly = shapely.set_precision(poly, grid_size=_SNAP_GRID_DEG)
    poly = _dedupe_ring(poly)

    if not poly.is_valid:
        fixed = make_valid(poly)
        if fixed.geom_type != "Polygon":
            candidates = [g for g in getattr(
                fixed, "geoms", [fixed]) if g.geom_type == "Polygon"]
            if not candidates:
                raise ValueError(
                    "Farm boundary has no valid polygonal area after cleanup")
            fixed = max(candidates, key=lambda g: g.area)
        poly = fixed

    if poly.is_empty or poly.area == 0:
        raise ValueError("Farm boundary has zero area after cleanup")

    return mapping(poly)


def _clean_ring_coords(coords: list[tuple]) -> list[tuple]:
    cleaned = [coords[0]]
    for pt in coords[1:]:
        if pt != cleaned[-1]:
            cleaned.append(pt)
    if cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])
    return cleaned


def _dedupe_ring(poly: Polygon) -> Polygon:
    exterior = _clean_ring_coords(list(poly.exterior.coords))
    if len(exterior) < 4:  # need >= 3 distinct points + closing point
        raise ValueError(
            "Farm boundary collapsed to fewer than 3 distinct vertices after cleanup")

    interiors = []
    for ring in poly.interiors:
        cleaned = _clean_ring_coords(list(ring.coords))
        if len(cleaned) >= 4:
            interiors.append(cleaned)

    return Polygon(exterior, interiors)
