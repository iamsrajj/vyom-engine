"""
storage — a thin abstraction over "where files live" so the rest of the codebase
(download_manager, processing pipelines) never calls open()/boto3 directly.

Two backends:
  - LocalStorage: writes to disk on this machine. Fine for a single-server setup.
  - S3Storage: writes to MinIO or any S3-compatible object store. This is what lets
    you run multiple worker machines (horizontal scaling) that all need access to
    the same raw downloads and processed COGs, and it's what a CDN/tile-serving
    layer would read from directly at higher traffic.

Selected via STORAGE_BACKEND in .env — nothing else in the codebase needs to change
when you switch.
"""
import logging
import os
import shutil
from abc import ABC, abstractmethod

from vyom.config import settings

logger = logging.getLogger("vyom.storage")


class Storage(ABC):
    @abstractmethod
    def save_raw(self, local_tmp_path: str, key: str) -> str:
        """Persist a downloaded file, return the path/URI to store in the DB."""

    @abstractmethod
    def save_processed(self, local_tmp_path: str, key: str) -> str:
        """Persist a processed COG, return the path/URI to store in the DB."""

    @abstractmethod
    def open_for_read(self, stored_path: str) -> str:
        """Return a local filesystem path usable by rasterio/GDAL for reading.
        For local storage this is a no-op; for S3 this downloads to a temp path
        (or returns a /vsis3/ GDAL virtual path — see note in S3Storage)."""

    @abstractmethod
    def ensure_local_copy(self, stored_path: str, dest_dir: str) -> str:
        """Return a path to a REAL local file for `stored_path`, downloading it
        first if it isn't one already. Unlike open_for_read -- which for S3 can
        hand back a /vsis3/ GDAL virtual path that only GDAL's own raster
        drivers know how to read -- this guarantees a plain path that ordinary
        Python file I/O (zipfile, open()) can use. Needed for extracting the
        raw SAFE.zip, which isn't a raster GDAL can stream out of a VFS."""

    @abstractmethod
    def delete(self, stored_path: str) -> None:
        """Remove a file — used to clean up raw downloads after processing,
        since they're always re-fetchable from Copernicus and are the single
        biggest disk consumer (doc's own storage-lifecycle guidance)."""


class LocalStorage(Storage):
    def __init__(self):
        os.makedirs(settings.raw_data_dir, exist_ok=True)
        os.makedirs(settings.processed_data_dir, exist_ok=True)

    def save_raw(self, local_tmp_path: str, key: str) -> str:
        dest = os.path.join(settings.raw_data_dir, key)
        # Don't assume this still exists just because __init__ created it once --
        # if it's ever removed while the process keeps running (manual cleanup,
        # a cron job, disk issues), shutil.move fails with a bare FileNotFoundError
        # that gives no hint the *directory* (not the file) is what's missing.
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if local_tmp_path != dest:
            shutil.move(local_tmp_path, dest)
        return dest

    def save_processed(self, local_tmp_path: str, key: str) -> str:
        dest = os.path.join(settings.processed_data_dir, key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if local_tmp_path != dest:
            shutil.move(local_tmp_path, dest)
        return dest

    def open_for_read(self, stored_path: str) -> str:
        return stored_path

    def ensure_local_copy(self, stored_path: str, dest_dir: str) -> str:
        # Already a plain path on this machine's disk -- nothing to download.
        return stored_path

    def delete(self, stored_path: str) -> None:
        if os.path.exists(stored_path):
            os.remove(stored_path)


class S3Storage(Storage):
    """
    Backed by MinIO or any S3-compatible store, via boto3. Processed COGs are read
    back by rio-tiler/rasterio using GDAL's /vsis3/ virtual filesystem so tile
    serving can do partial range-reads directly from object storage without
    downloading the whole file first — this is what keeps tile-serving fast once
    you're not on local disk anymore.
    """

    def __init__(self):
        import boto3

        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            use_ssl=settings.s3_use_ssl,
        )
        for bucket in (settings.s3_bucket_raw, settings.s3_bucket_processed):
            try:
                self._client.head_bucket(Bucket=bucket)
            except Exception:  # noqa: BLE001 — bucket doesn't exist yet, create it
                logger.info("Creating bucket %s", bucket)
                self._client.create_bucket(Bucket=bucket)

        # GDAL env vars so /vsis3/ virtual paths can authenticate against MinIO
        os.environ.setdefault("AWS_S3_ENDPOINT", settings.s3_endpoint_url.replace(
            "http://", "").replace("https://", ""))
        os.environ.setdefault("AWS_ACCESS_KEY_ID", settings.s3_access_key)
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", settings.s3_secret_key)
        os.environ.setdefault("AWS_VIRTUAL_HOSTING", "FALSE")
        os.environ.setdefault(
            "AWS_HTTPS", "YES" if settings.s3_use_ssl else "NO")

    def save_raw(self, local_tmp_path: str, key: str) -> str:
        self._client.upload_file(local_tmp_path, settings.s3_bucket_raw, key)
        os.remove(local_tmp_path)
        return f"s3://{settings.s3_bucket_raw}/{key}"

    def save_processed(self, local_tmp_path: str, key: str) -> str:
        self._client.upload_file(
            local_tmp_path, settings.s3_bucket_processed, key)
        os.remove(local_tmp_path)
        return f"s3://{settings.s3_bucket_processed}/{key}"

    def open_for_read(self, stored_path: str) -> str:
        if stored_path.startswith("s3://"):
            # GDAL virtual filesystem path — rasterio/rio-tiler can read this directly
            # (partial reads over HTTP range requests), no download needed.
            without_scheme = stored_path[len("s3://"):]
            return f"/vsis3/{without_scheme}"
        return stored_path

    def ensure_local_copy(self, stored_path: str, dest_dir: str) -> str:
        if not stored_path.startswith("s3://"):
            return stored_path
        bucket, key = stored_path[len("s3://"):].split("/", 1)
        os.makedirs(dest_dir, exist_ok=True)
        local_path = os.path.join(dest_dir, os.path.basename(key))
        logger.info(
            "Downloading %s to local temp %s for zip extraction",
            stored_path, local_path,
        )
        self._client.download_file(bucket, key, local_path)
        return local_path

    def delete(self, stored_path: str) -> None:
        if stored_path.startswith("s3://"):
            bucket, key = stored_path[len("s3://"):].split("/", 1)
            self._client.delete_object(Bucket=bucket, Key=key)
        elif os.path.exists(stored_path):
            os.remove(stored_path)


def get_storage() -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage()
    return LocalStorage()


# Module-level singleton, same pattern as auth_broker — one storage client shared
# across the process rather than reconstructed per call.
storage = get_storage()
