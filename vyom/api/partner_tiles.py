"""api/partner_tiles -- point 5 of the monetization spec: let a business
integrator plot a farm's satellite indices on their OWN map (Leaflet,
Mapbox GL, Google Maps, etc.), not just read raw numbers.

Two routes, two different auth schemes, on purpose (see
vyom/api_auth.py's "Map-tile tokens" section for the full reasoning):

  GET /api/v1/farms/{farm_id}/map-layer
      Normal X-Api-Key/X-Api-Secret header auth, same as every other
      partner-api route. Returns everything needed to add a tile layer to
      a map: a ready-to-use XYZ tile URL template (token already baked
      in), the farm's bounds/center, which indices exist per platform,
      and a legend per index so the integrator doesn't have to
      reverse-engineer this deployment's color scale.

  GET /api/v1/farms/{farm_id}/map/{platform}/{index}/{date}/{z}/{x}/{y}.png
      Token auth via ?token=... (see require_map_tile_token) -- NOT
      X-Api-Key/X-Api-Secret, because every map library loads XYZ tiles
      as plain image requests with no custom headers available. This is
      the same header-vs-query-token split the dashboard's own /tiles
      router already uses (vyom/auth.py: require_auth vs
      require_auth_query) -- this router is the partner-API-scoped
      equivalent of that pattern, reusing tiles.py's own rendering code
      (render_index_tile) so both surfaces stay pixel-for-pixel
      identical and any future rendering fix applies to both at once.
"""
import uuid
from datetime import date as date_cls
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from geoalchemy2.shape import to_shape
from pydantic import BaseModel
from sqlalchemy.orm import Session

from vyom.api.partner_farms import Envelope, Meta, _build_meta, _get_owned_api_farm
from vyom.api.tiles import get_index_render_config, render_index_tile
from vyom.api_auth import (
    ApiV1Error,
    BusinessApiContext,
    MapTileTokenContext,
    issue_map_tile_token,
    require_business_api_auth,
    require_map_tile_token,
)
from vyom.config import settings
from vyom.db import get_db
from vyom.models import Polygon
from vyom.processing.index_scale import INDEX_SCALES, scales_for_api

router = APIRouter(prefix="/api/v1/farms", tags=["partner-api"])


class TileLegendBand(BaseModel):
    upper: Optional[float]
    tier: str
    color: str


class IndexLegend(BaseModel):
    platform: str
    # "discrete" -- a fixed set of labelled color bands (see
    # vyom/processing/index_scale.py), matches what the dashboard itself
    # shows. "continuous" -- a plain min/max colormap, for any index
    # index_scale.py has no defined bands for yet (currently just
    # VV_VH_RATIO).
    scale_type: str
    colormap: str
    range: list[float]
    bands: Optional[list[TileLegendBand]] = None


class MapLayerOut(BaseModel):
    tile_url_template: str
    token_expires_at: str
    bounds: list[list[float]]  # [[south, west], [north, east]]
    center: list[float]        # [lat, lon]
    min_zoom: int
    max_zoom: int
    platforms: dict[str, list[str]]
    legend: dict[str, IndexLegend]


def _legend_for_index(index_name: str, platform: str) -> IndexLegend:
    cfg = get_index_render_config().get(index_name)
    if cfg is None:
        # Shouldn't happen for anything in settings.s2_indices/s1_indices --
        # tiles.py's _INDEX_RENDER_CONFIG is required to have an entry for
        # every enabled index (see that file's own comment on this). Fall
        # back to a safe default rather than 500ing the whole map-layer
        # response over one misconfigured index.
        cfg = {"colormap": "viridis", "range": (0, 1)}
    bands = INDEX_SCALES.get(index_name)
    if bands:
        return IndexLegend(
            platform=platform, scale_type="discrete",
            colormap=cfg["colormap"], range=list(cfg["range"]),
            bands=[TileLegendBand(upper=upper, tier=tier, color=color)
                   for upper, tier, color in bands],
        )
    return IndexLegend(
        platform=platform, scale_type="continuous",
        colormap=cfg["colormap"], range=list(cfg["range"]),
    )


@router.get("/{farm_id}/map-layer", response_model=Envelope[MapLayerOut])
def get_partner_farm_map_layer(
    farm_id: uuid.UUID,
    ctx: BusinessApiContext = Depends(require_business_api_auth),
    db: Session = Depends(get_db),
):
    """Everything needed to add this farm's satellite indices as a tile
    layer on your own map. Call this once per farm (tokens are valid for
    24h -- call it again to refresh the URL once it expires, rather than
    trying to reuse an expired token), then feed `tile_url_template`
    straight into your map library's XYZ/raster tile layer, substituting
    {platform}, {index}, {date}, {z}, {x}, {y} yourself (`date` is either
    an ISO date from GET /{farm_id}/available-dates, or the literal string
    'latest'). `legend` gives you the exact colors this deployment renders
    for each index, so your own map legend matches what the tiles show.
    """
    farm = _get_owned_api_farm(db, farm_id, ctx)

    token, expires_at = issue_map_tile_token(farm.id, ctx.credential.id)
    tile_url_template = (
        f"{settings.dashboard_base_url}/api/v1/farms/{farm.id}"
        f"/map/{{platform}}/{{index}}/{{date}}/{{z}}/{{x}}/{{y}}.png?token={token}"
    )

    shape_geom = to_shape(farm.geom)
    minx, miny, maxx, maxy = shape_geom.bounds
    centroid = shape_geom.centroid

    platforms = {"S2": list(settings.s2_indices),
                 "S1": list(settings.s1_indices)}
    legend = {}
    for idx in platforms["S2"]:
        legend[idx] = _legend_for_index(idx, "S2")
    for idx in platforms["S1"]:
        legend[idx] = _legend_for_index(idx, "S1")

    from datetime import datetime, timezone
    data = MapLayerOut(
        tile_url_template=tile_url_template,
        token_expires_at=datetime.fromtimestamp(
            expires_at, tz=timezone.utc).isoformat(),
        bounds=[[miny, minx], [maxy, maxx]],
        center=[centroid.y, centroid.x],
        min_zoom=10, max_zoom=18,
        platforms=platforms, legend=legend,
    )
    return Envelope(data=data, meta=_build_meta(db, ctx))


@router.get("/{farm_id}/map/{platform}/{index}/{date}/{z}/{x}/{y}.png")
def get_partner_farm_map_tile(
    farm_id: uuid.UUID, platform: str, index: str, date: str, z: int, x: int, y: int,
    include_interpolated: bool = True,
    token_ctx: MapTileTokenContext = Depends(require_map_tile_token),
    db: Session = Depends(get_db),
):
    """The actual tile pixels -- never called directly by an integrator,
    only by their map library after it's been handed the
    `tile_url_template` from GET /{farm_id}/map-layer. Token-scoped to
    exactly this farm_id (see require_map_tile_token): a token minted for
    one farm cannot be reused for another, even by editing the URL.

    include_interpolated defaults to true here (unlike the dashboard's own
    /tiles endpoint, which defaults to false) since a partner map layer
    showing large blank/404 gaps between real satellite passes makes for
    a much worse map widget than the dashboard's date-picker context,
    where a farmer can just see there's no real reading for that day.
    Every tile response still carries the same X-Vyom-Data-Source header
    ("satellite" | "interpolated" | "provisional") this deployment always
    uses -- read it if your integration needs to distinguish real
    readings from filled ones, exactly like GET /{farm_id}/available-dates'
    own `source` field does for the scalar timeseries.
    """
    if token_ctx.farm_id != farm_id:
        raise ApiV1Error(
            403, "TOKEN_SCOPE_MISMATCH",
            "This map tile token was issued for a different farm. Call GET "
            "/api/v1/farms/{farm_id}/map-layer for this farm to get a valid token.")

    farm = db.get(Polygon, farm_id)
    if farm is None or farm.deleted_at is not None:
        raise ApiV1Error(404, "NOT_FOUND", "Farm not found")

    content, source_label = render_index_tile(
        db, farm, date, z, x, y, index, platform, include_interpolated)

    return Response(
        content=content, media_type="image/png",
        headers={"X-Vyom-Data-Source": source_label},
    )
