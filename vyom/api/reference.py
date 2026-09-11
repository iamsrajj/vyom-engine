"""
reference.py -- server-side proxy for NovosEdge's public crop/soil
reference lists (used by the crop/soil pickers in the "Draw new field"
modal, see web/index.html openCropPicker()/openSoilPicker()).

Why this proxy exists instead of the browser calling NovosEdge directly:
1. CORS -- api.novosedge.xyz is built for server-to-server use (custom
   port + x-api-key header), and doesn't set Access-Control-Allow-Origin
   for browser JS, so a direct fetch() from the dashboard fails outright
   regardless of whether the key is correct.
2. Key exposure -- proxying keeps NOVOSEDGE_API_KEY server-side only,
   instead of shipping it in browser-visible JS.

Cached in-process with a simple TTL, since this reference data changes
rarely (new crop/soil entries added occasionally, not a live feed) --
every farmer opening the picker shouldn't cause a fresh upstream call.
This is a single-process in-memory cache (fine for now, same as the rest
of Phase 1's monolith design) -- if this ever runs behind multiple API
processes, each will warm its own copy independently, which is harmless
here (worst case: a few redundant upstream calls after a deploy).
"""
import logging
import time

import requests
from fastapi import APIRouter, HTTPException, Response

from vyom.config import settings

logger = logging.getLogger("vyom.reference")
router = APIRouter(prefix="/reference", tags=["reference"])

# not tied to poll_all_farms -- just a sane refresh interval
_CACHE_TTL_SECONDS = 6 * 3600
_cache: dict[str, tuple[float, list]] = {}


def _fetch_novosedge_list(path: str, list_key: str) -> tuple[list, bool]:
    """Returns (data, was_cache_hit) -- callers set X-Cache from the second
    element (same convention as farms.py's _cached_read) so this is directly
    observable rather than inferred from latency."""
    now = time.time()
    cached = _cache.get(path)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1], True

    if not settings.novosedge_api_key:
        raise HTTPException(
            503, "NOVOSEDGE_API_KEY is not configured on the server (see .env)")

    try:
        resp = requests.get(
            f"{settings.novosedge_api_base}{path}",
            headers={"x-api-key": settings.novosedge_api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get(list_key, [])
    except requests.RequestException as exc:
        if cached:
            # NovosEdge is briefly unreachable but we have something to
            # serve -- stale reference data beats a broken picker.
            logger.warning(
                "NovosEdge %s fetch failed (%s), serving stale cache", path, exc)
            return cached[1], True
        logger.error("NovosEdge %s fetch failed: %s", path, exc)
        raise HTTPException(
            502, f"Could not reach the crop/soil reference service: {exc}")

    _cache[path] = (now, data)
    return data, False


@router.get("/crops")
def list_crops(response: Response):
    # Cache-Control: paired with the frontend's own localStorage cache (see
    # fetchCropList() in web/index.html) -- this lets the browser skip the
    # round-trip to us entirely for a while, on top of us skipping the
    # round-trip to NovosEdge (the in-process cache above).
    response.headers["Cache-Control"] = "private, max-age=21600"
    data, was_hit = _fetch_novosedge_list("/ad/crop/list", "cropList")
    response.headers["X-Cache"] = "HIT" if was_hit else "MISS"
    return {"cropList": data}


@router.get("/soils")
def list_soils(response: Response):
    response.headers["Cache-Control"] = "private, max-age=21600"
    data, was_hit = _fetch_novosedge_list("/ad/crop/soil", "soilList")
    response.headers["X-Cache"] = "HIT" if was_hit else "MISS"
    return {"soilList": data}
