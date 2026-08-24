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
from vyom.error_log import log_error
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


def _fetch_all_pages(url: str, params: dict, platform: str, max_pages: int = 20) -> list[dict]:
    """CDSE's OData API defaults to $top=20 and provides an @odata.nextLink
    when more results exist (documented max $top is 1000, but nextLink
    pagination is the correct general mechanism regardless of $top size --
    see https://documentation.dataspace.copernicus.eu/APIs/OData.html#top-option).
    The previous version of this function used a bare $top=20 with no
    pagination at all, silently truncating to the 20 most recent products for
    any query that matched more than that -- a real, quiet data-loss bug for
    any days_back window wide enough to contain >20 products (easy to hit:
    S1 alone can produce 20+ products in a couple months even at reduced
    revisit). This follows nextLink until either results are exhausted or
    max_pages is hit (safety cap against a runaway query -- CDSE's own advice
    for very wide date ranges is to split into smaller windows rather than
    paginate indefinitely, so hitting this cap should prompt narrowing
    days_back, not raising max_pages further)."""
    all_results = []
    next_url = url
    next_params = params
    for page in range(max_pages):
        resp = cdse_request("GET", next_url, params=next_params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        all_results.extend(body.get("value", []))

        next_link = body.get("@odata.nextLink")
        if not next_link:
            return all_results
        next_url = next_link
        next_params = None  # nextLink is already a complete URL with its own query string

    log_error(
        "discovery",
        f"Hit max_pages ({max_pages}) while paginating CDSE results -- more data may exist. "
        f"Consider narrowing days_back for this query instead of raising max_pages.",
        level="warning", platform=platform,
        context={"url": url}, include_traceback=False,
    )
    return all_results


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

    For S2 specifically: queries L2A (S2MSI2A) first, and separately checks for
    any L1C (S2MSI1C) scenes that exist for the same window with NO matching L2A
    counterpart -- this surfaces "ESA published the scene but L2A atmospheric
    correction hasn't caught up yet" as a distinct, logged, diagnosable case,
    rather than that scene just silently looking like a missing date. (This
    pipeline still only processes L2A -- L1C-only scenes are logged, not
    downloaded -- since the indices in indices.py assume L2A surface reflectance.)

    Returns the list of CatalogProduct rows (new + already-known) intersecting
    this geometry, so callers can build polygon_tile_map without a second query.
    """
    collection, product_type = _platform_query_params(platform)
    poly = shape(geometry)
    aoi_wkt = poly.wkt

    date_from = (datetime.now(timezone.utc) -
                 timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _build_filter(product_type_override: str) -> str:
        parts = [
            f"Collection/Name eq '{collection}'",
            f"Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq '{product_type_override}')",
            f"OData.CSC.Intersects(area=geography'SRID=4326;{aoi_wkt}')",
            f"ContentDate/Start gt {date_from}",
            f"ContentDate/Start lt {date_to}",
        ]
        if platform == "S2" and product_type_override == settings.s2_product_type:
            max_cc = max_cloud_cover if max_cloud_cover is not None else settings.default_max_cloud_cover
            parts.append(
                f"Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover' and att/OData.CSC.DoubleAttribute/Value le {max_cc})"
            )
        return " and ".join(parts)

    url = f"{settings.cdse_odata_url}/Products"
    params = {
        "$filter": _build_filter(product_type),
        "$orderby": "ContentDate/Start desc",
        "$top": 100,  # nextLink pagination below handles anything beyond this
        "$expand": "Attributes",
    }

    results = _fetch_all_pages(url, params, platform)
    logger.info("CDSE discovery (%s) returned %d candidate product(s) across all pages",
                platform, len(results))

    # S2-only: diagnose L2A-missing-but-L1C-exists gaps, per ESA's own
    # documented processing-lag/anomaly behavior (see research notes) --
    # doesn't download anything, just makes an otherwise-silent gap visible.
    if platform == "S2":
        l1c_filter = _build_filter(settings.s2_l1c_product_type)
        l1c_params = {"$filter": l1c_filter,
                      "$orderby": "ContentDate/Start desc", "$top": 100}
        l1c_results = _fetch_all_pages(url, l1c_params, platform, max_pages=5)
        l2a_dates = {item["ContentDate"]["Start"][:10] for item in results}
        l1c_only_dates = {item["ContentDate"]["Start"][:10]
                          for item in l1c_results} - l2a_dates
        if l1c_only_dates:
            logger.warning(
                "S2 L1C exists but no matching L2A for %d date(s): %s -- "
                "ESA's L2A atmospheric correction may still be catching up, "
                "or this baseline never got L2A processing.",
                len(l1c_only_dates), sorted(l1c_only_dates),
            )
            log_error(
                "discovery", f"L1C exists without L2A for {len(l1c_only_dates)} date(s)",
                level="warning", platform="S2",
                context={"dates": sorted(l1c_only_dates)}, include_traceback=False,
            )

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
