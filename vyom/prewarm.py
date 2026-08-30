"""
prewarm -- deliberate, targeted pre-fetching for a known upcoming rollout
(e.g. "we're onboarding District X next month"). Explicitly NOT automatic
national coverage -- see the earlier item-8 research: blind pan-India
pre-processing wastes compute/storage on land with no farmers, and CDSE's
hard connection-rate limits don't care how much infrastructure you throw at
the problem. This tool exists for the narrow, deliberate case where you
genuinely know farmers are coming to a specific area soon.

Reuses the exact same reuse-check + cold-start-buffered refresh machinery
every real farm already goes through (see farms.py's
_backfill_and_dispatch_refresh) -- no separate fetch pipeline. A target
region (usually much bigger than one farm's cluster window) gets tiled into
a grid of cluster-sized cells; each cell becomes a synthetic seed Polygon
(is_prewarm_seed=True, never a real farm) whose only purpose is to trigger
processing for that cell and leave real processed_bounds coverage behind --
coverage that persists independently of the seed polygon itself (reuse-check
reads processed_bounds off CatalogProduct rows directly, not "is a farm
still linked here"), so seeds can be deleted later without undoing the
benefit.

Every seed's refresh is dispatched with priority=False, deliberately --
prewarm is background/non-urgent work by definition and must never compete
with a real farmer's onboarding request for the priority queues' dedicated
capacity. CDSE's global rate limiter (cdse_rate_limiter.py) naturally paces
however many cells get dispatched at once; no extra throttling is needed
here.
"""
import logging
import uuid
from datetime import date

from shapely.geometry import shape, box, mapping
from shapely.ops import unary_union
from geoalchemy2.shape import from_shape
from sqlalchemy.orm import Session

from vyom.models import Polygon
from vyom.config import settings

logger = logging.getLogger("vyom.prewarm")

# Spacing between adjacent cell centers. Deliberately smaller than
# 2*cold_start_buffer_deg so neighboring cells' windows overlap generously
# rather than risk gaps at the edges -- better to overlap (a little wasted
# reprocessing at boundaries) than leave a real gap a farmer's polygon could
# fall into. Default cell size independent of cold_start_buffer_deg on
# purpose: pass a different cell_size_deg explicitly for a denser or sparser
# grid than the single-farm cold-start default.
# ~4.4km at the equator, same approximation caveat as cold_start_buffer_deg
DEFAULT_CELL_SIZE_DEG = 0.04


def generate_grid_cells(region_geometry: dict, cell_size_deg: float = DEFAULT_CELL_SIZE_DEG) -> list:
    """region_geometry: GeoJSON Polygon/MultiPolygon for the target area
    (e.g. a district boundary). Returns a list of shapely box geometries
    tiling the region's bounding box, keeping only cells that actually
    intersect the real region shape (not just its bounding box) -- avoids
    dispatching pointless cells for a very non-rectangular region (e.g. a
    long thin district) whose bounding box is mostly outside its real
    boundary."""
    region_shape = shape(region_geometry)
    minx, miny, maxx, maxy = region_shape.bounds

    cells = []
    x = minx
    while x < maxx:
        y = miny
        while y < maxy:
            cell = box(x, y, x + cell_size_deg, y + cell_size_deg)
            if cell.intersects(region_shape):
                cells.append(cell)
            y += cell_size_deg
        x += cell_size_deg

    return cells


def prewarm_region(db: Session, region_geometry: dict, user_id: uuid.UUID,
                   cell_size_deg: float = DEFAULT_CELL_SIZE_DEG, region_name: str = "prewarm") -> dict:
    """Tiles region_geometry into cells, creates a seed Polygon for each,
    and dispatches its (non-priority) refresh via the normal
    reuse-check + cold-start machinery. Returns a summary dict: how many
    cells were generated and how many seeds were actually created (a cell
    already fully covered by existing data still gets a seed + dispatch --
    reuse-check inside the dispatch will correctly find it's already
    covered and skip any real CDSE work for it, so no special-casing is
    needed here to avoid redundant fetches)."""
    # Imported here, not at module load, to avoid a circular import --
    # farms.py imports from prewarm.py's sibling modules at startup, and
    # _backfill_and_dispatch_refresh lives in farms.py itself.
    from vyom.api.farms import _backfill_and_dispatch_refresh, _geodesic_area_ha

    cells = generate_grid_cells(region_geometry, cell_size_deg)
    seeds_created = 0

    for i, cell in enumerate(cells):
        seed = Polygon(
            name=f"{region_name} (seed {i+1}/{len(cells)})",
            user_id=user_id,
            geom=from_shape(cell, srid=4326),
            area_ha=_geodesic_area_ha(cell),
            is_draft=True,
            is_prewarm_seed=True,
        )
        db.add(seed)
        db.commit()
        db.refresh(seed)

        _backfill_and_dispatch_refresh(db, seed, priority=False)
        seeds_created += 1

    logger.info("Prewarm '%s': tiled into %d cell(s), %d seed(s) dispatched (priority=False)",
                region_name, len(cells), seeds_created)
    return {"region_name": region_name, "cells_generated": len(cells), "seeds_dispatched": seeds_created}
