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
import time

import requests
from sqlalchemy.orm import Session

from vyom.auth_broker import auth_broker
from vyom.cdse_rate_limiter import (
    acquire_connection_slot, release_connection_slot, renew_slot_lease,
    wait_for_rate_slot, CdseRateLimitExceeded, CdseLeaseLost,
)
from vyom.models import CatalogProduct
from vyom.storage import storage
from vyom.error_log import log_error

logger = logging.getLogger("vyom.download_manager")

_MAX_REDIRECTS = 5
# heartbeat well inside LEASE_TTL_SECONDS (90s)
_LEASE_RENEW_INTERVAL_SECONDS = 20
# inline retry for transient connection drops mid-download
_MAX_STREAM_RETRIES = 3


def _get_following_redirects_with_auth(url: str, headers: dict, timeout) -> tuple[requests.Response, str]:
    """requests.get(..., allow_redirects=True) drops the Authorization header on
    any redirect to a different host, which is exactly what download.dataspace.
    copernicus.eu does -- it 302s to a signed node/object-storage URL. CDSE's own
    docs handle this with curl's `--location-trusted` (forward auth across the
    redirect); this is the requests equivalent, done manually so we keep control
    of the final streaming response instead of letting requests re-issue and
    buffer it internally.

    Holds ONE CDSE connection slot for the full duration the caller streams the
    body -- not just for this initial request. Returns (response, lease_id);
    the caller must call renew_slot_lease(lease_id) periodically while still
    streaming (see download_product) and release_connection_slot(lease_id)
    exactly once when done.

    Slot handling contract: if this function raises, no slot is held (any
    slot acquired internally is released before the exception propagates)."""
    lease_id = acquire_connection_slot()
    if lease_id is None:
        raise CdseRateLimitExceeded(
            "Timed out waiting for a free CDSE connection slot for download")
    try:
        wait_for_rate_slot()

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
            return resp, lease_id  # slot ownership now transfers to the caller
        raise RuntimeError(f"Too many redirects downloading {url}")
    except Exception:
        release_connection_slot(lease_id)  # not returning it to the caller
        raise


def _sha3_256_of_file(path: str) -> str:
    h = hashlib.sha3_256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stream_download_once(download_url: str, tmp_path: str):
    """One attempt at connect+stream+write. Holds a connection slot for the
    whole transfer, sending a heartbeat renewal every
    _LEASE_RENEW_INTERVAL_SECONDS so a legitimately long download's slot
    never expires just because no other worker happened to poll Redis in the
    meantime (that was a real bug in the previous shared-TTL-counter design
    -- each lease now has its own expiry that only ITS holder can extend).
    Raises CdseLeaseLost if the lease somehow got dropped mid-stream (should
    be rare given the 90s TTL vs 20s renewal cadence, but the download must
    not be trusted as "holding a slot" if it happens)."""
    resp, lease_id = _get_following_redirects_with_auth(
        download_url, auth_broker.auth_header(), timeout=(30, 300))
    try:
        with resp:
            last_renew = time.time()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
                    if time.time() - last_renew > _LEASE_RENEW_INTERVAL_SECONDS:
                        # raises CdseLeaseLost if it's gone
                        renew_slot_lease(lease_id)
                        last_renew = time.time()
    finally:
        release_connection_slot(lease_id)


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
        # Inline retry for transient mid-stream failures (dropped connection,
        # read timeout, lease lost) -- cheaper than falling all the way back
        # to Celery's task-level retry (which re-queues the whole farm
        # refresh and waits for the next scheduled attempt) for something
        # that's often just a momentary network blip.
        last_exc = None
        for attempt in range(1, _MAX_STREAM_RETRIES + 1):
            try:
                _stream_download_once(download_url, tmp_path)
                last_exc = None
                break
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout,
                    CdseLeaseLost) as exc:
                last_exc = exc
                logger.warning(
                    "Transient failure downloading %s (attempt %d/%d): %s",
                    product.product_name, attempt, _MAX_STREAM_RETRIES, exc,
                )
                if attempt < _MAX_STREAM_RETRIES:
                    # brief linear backoff between stream attempts
                    time.sleep(2 * attempt)
        if last_exc is not None:
            raise last_exc

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

    except CdseRateLimitExceeded as exc:
        # Expected/transient congestion, not a bug -- logged as a warning so
        # it doesn't clutter the Errors panel the same way a real failure
        # would. The product stays 'discovered' (not 'failed'), so the next
        # scheduled refresh naturally retries it.
        logger.warning("CDSE rate limit hit downloading %s: %s",
                       product.product_name, exc)
        log_error("download_manager", str(exc), level="warning", platform=product.platform,
                  context={"product_id": str(product.id), "product_name": product.product_name})
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

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
