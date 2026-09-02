import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from vyom.auth import require_error_panel_access, stable_owner_uuid
from vyom.db import get_db
from vyom.prewarm import prewarm_region, DEFAULT_CELL_SIZE_DEG

router = APIRouter(prefix="/admin/prewarm", tags=["admin"],
                   dependencies=[Depends(require_error_panel_access)])


class PrewarmRequest(BaseModel):
    region_geometry: dict = Field(...,
                                  description="GeoJSON Polygon/MultiPolygon of the target region (e.g. a district boundary)")
    # user_id REMOVED (consistency fix, same reasoning as farms.py's BOLA
    # fix): seed ownership is now derived from the authenticated admin
    # actually making this call, not trusted from client input.
    region_name: str = "prewarm"
    cell_size_deg: float = DEFAULT_CELL_SIZE_DEG


@router.post("")
def prewarm(payload: PrewarmRequest, current_user: str = Depends(require_error_panel_access), db: Session = Depends(get_db)):
    """Deliberate, targeted pre-fetching for a known upcoming rollout --
    NOT automatic national coverage (see vyom/prewarm.py's module docstring
    for why that was explicitly ruled out). Tiles region_geometry into
    cluster-sized cells and dispatches a non-priority refresh for each,
    through the exact same reuse-check + cold-start machinery every real
    farm already uses. Every dispatched fetch is priority=False, on purpose
    -- this must never compete with a real farmer's onboarding request for
    the priority queues' dedicated capacity.

    cell_size_deg defaults to ~4.4km -- pass a smaller value for denser
    (more, smaller, more overlapping) coverage, or larger for a coarser
    sweep over a big region. Returns immediately; the actual fetches
    continue in the background across the normal (non-priority) queues,
    naturally paced by CDSE's global rate limiter (cdse_rate_limiter.py) --
    a large region will take real time to fully cover, proportional to how
    many cells it tiles into."""
    return prewarm_region(
        db, payload.region_geometry, stable_owner_uuid(current_user),
        cell_size_deg=payload.cell_size_deg, region_name=payload.region_name,
    )
