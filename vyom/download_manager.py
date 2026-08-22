"""
download_manager -- downloads a discovered Copernicus product to a temp path,
verifies its checksum, then hands it to the storage abstraction (local disk or
S3/MinIO -- see storage.py) and marks it 'downloaded' in catalog_products.

Dedup rule: if status is already 'downloaded' or 'processed', this is a no-op.
"""
import hashlib
import logging
import os
import tempfile

import requests
from sqlalchemy.orm import Session

from vyom.auth_broker import auth_broker
from vyom.models import CatalogProduct
from vyom.storage import storage
from vyom.error_log import log_error

logger = logging.getLogger("vyom.download_manager")

_DOWNLOAD_URLS = {
    "S2": None,  # resolved from settings.cdse_download_url per-call, same endpoint for both platforms
}

_MAX_REDIRECTS = 5


def _get_following_redirects_with_auth(url: str, headers: dict, timeout) -> requests.Response:
    """requests.get(..., allow_redirects=True) drops the Authorization header on
    any redirect to a different host, which is exactly what download.dataspace.
    copernicus.eu does -- it 302s to a signed node/object-storage URL. CDSE's own
    docs handle this with curl's `--location-trusted` (forward auth across the
    redirect); this is the requests equivalent, done manually so we keep control
    of the final streaming response instead of letting requests re-issue and
    buffer it internally."""
    session = requests.Session()
    current_url = url
    for _ in range(_MAX_REDIRECTS):
        resp = session.get(current_url, headers=headers,
                           stream=True, timeout=timeout, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                resp.raise_for_status()
            current_url = location
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"Too many redirects downloading {url}")


def _sha3_256_of_file(path: str) -> str:
    h = hashlib.sha3_256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_product(db: Session, product: CatalogProduct) -> CatalogProduct:
    """Downloads `product` if not already downloaded/processed, then persists it
    via the configured storage backend. Returns the updated CatalogProduct."""
    if product.status in ("downloaded", "processed"):
        logger.info("Product %s already %s, skipping download",
                    product.product_name, product.status)
        return product

    from vyom.config import settings
    download_url = f"{settings.cdse_download_url}/Products({product.product_id})/$value"

    fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="vyom_dl_")
    os.close(fd)

    logger.info("Downloading %s (%s)", product.product_name, product.platform)

    try:
        with _get_following_redirects_with_auth(
            download_url, auth_broker.auth_header(), timeout=(30, 300)
        ) as resp:
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)

        checksum = _sha3_256_of_file(tmp_path)
        stored_path = storage.save_raw(tmp_path, f"{product.product_name}.zip")

        product.raw_path = stored_path
        product.checksum = checksum
        product.status = "downloaded"
        product.error_message = None
        db.add(product)
        db.commit()
        db.refresh(product)

        logger.info("Downloaded %s -> %s (%s)",
                    product.product_name, stored_path, checksum[:12])
        return product

    except Exception as exc:  # noqa: BLE001
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        product.status = "failed"
        product.error_message = str(exc)[:1000]
        db.add(product)
        db.commit()
        logger.exception("Download failed for %s", product.product_name)
        log_error("download_manager", str(exc), platform=product.platform,
                  context={"product_id": str(product.id), "product_name": product.product_name})
        raise
