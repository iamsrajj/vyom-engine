"""
discovery -- queries Copernicus Data Space Ecosystem for products intersecting a
farm polygon, for either platform ("S2" optical or "S1" SAR), and writes newly
found products into catalog_products (status='discovered'). This table is the
dedup source of truth -- a product is never queued for download twice.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import requests
from shapely.geometry import shape
from geoalchemy2.shape import from_shape
from sqlalchemy.orm import Session
from sqlalchemy import select

from vyom.config import settings
from vyom.auth_broker import auth_broker
from vyom.cdse_rate_limiter import cdse_request
from vyom.models import CatalogProduct

logger = logging.getLogger("vyom.discovery")

_S2_TILE_RE = re.compile(r"_T(\d{2}[A-Z]{3})_")
# relative orbit + mission data-take id fragment
_S1_TILE_RE = re.compile(r"_([0-9A-F]{6})_")


def _extract_tile_id(product_name: str, platform: str) -> str | None:
    pattern = _S2_TILE_RE if platform == "S2" else _S1_TILE_RE
    m = pattern.search(product_name)
    return m.group(1) if m else None


def _platform_query_params(platform: str) -> tuple[str, str]:
    if platform == "S1":
        return settings.s1_collection, settings.s1_product_type
    return settings.s2_collection, settings.s2_product_type


def discover_products_for_geometry(
    db: Session,
    geometry: dict,
    platform: str = "S2",
    days_back: int = 30,
    max_cloud_cover: float | None = None,
) -> list[CatalogProduct]:
    """
    Query CDSE for products of the given platform intersecting `geometry` (GeoJSON
    dict) within the last `days_back` days. For S2, filters by max_cloud_cover; S1
    has no cloud-cover concept (SAR sees through cloud) so that filter is skipped.

    Returns the list of CatalogProduct rows (new + already-known) intersecting
    this geometry, so callers can build polygon_tile_map without a second query.
    """
    collection, product_type = _platform_query_params(platform)
    poly = shape(geometry)
    aoi_wkt = poly.wkt

    date_from = (datetime.now(timezone.utc) -
                 timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    filter_parts = [
        f"Collection/Name eq '{collection}'",
        f"Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq '{product_type}')",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{aoi_wkt}')",
        f"ContentDate/Start gt {date_from}",
        f"ContentDate/Start lt {date_to}",
    ]
    if platform == "S2":
        max_cc = max_cloud_cover if max_cloud_cover is not None else settings.default_max_cloud_cover
        filter_parts.append(
            f"Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover' and att/OData.CSC.DoubleAttribute/Value le {max_cc})"
        )

    odata_filter = " and ".join(filter_parts)

    url = f"{settings.cdse_odata_url}/Products"
    params = {
        "$filter": odata_filter,
        "$orderby": "ContentDate/Start desc",
        "$top": 20,
        "$expand": "Attributes",
    }

    resp = cdse_request("GET", url, params=params, timeout=60)
    resp.raise_for_status()
    results = resp.json().get("value", [])

    logger.info("CDSE discovery (%s) returned %d candidate product(s)",
                platform, len(results))

    matched: list[CatalogProduct] = []

    for item in results:
        product_id = item["Id"]
        product_name = item["Name"]
        content_date = item["ContentDate"]["Start"]
        footprint_geojson = item.get("GeoFootprint")

        cloud_cover_val = None
        if platform == "S2":
            for attr in item.get("Attributes", []):
                if attr.get("Name") == "cloudCover":
                    cloud_cover_val = attr.get("Value")
                    break

        existing = db.execute(
            select(CatalogProduct).where(
                CatalogProduct.product_id == product_id)
        ).scalar_one_or_none()

        if existing:
            matched.append(existing)
            continue

        footprint_shape = shape(
            footprint_geojson) if footprint_geojson else poly

        record = CatalogProduct(
            platform=platform,
            collection=collection,
            product_id=product_id,
            product_name=product_name,
            tile_id=_extract_tile_id(product_name, platform),
            acquisition_date=content_date,
            cloud_cover=cloud_cover_val,
            footprint=from_shape(footprint_shape, srid=4326),
            status="discovered",
        )
        db.add(record)
        db.flush()
        matched.append(record)
        logger.info("Discovered new %s product %s", platform, product_name)

    db.commit()
    return matched
