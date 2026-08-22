"""
error_log -- single choke point every failure across discovery, download,
processing, zonal stats, Celery tasks, and the API routes writes through.
This is what backs the dashboard's Errors panel: one table, one query,
instead of grepping journalctl/Celery worker logs across five different
processes to piece together what actually broke.

Usage (inside an except block, alongside the existing logger.exception call
-- this does not replace local logging, it adds a queryable record of it):

    from vyom.error_log import log_error

    except Exception as exc:
        logger.exception("S1 processing failed for %s", product.product_name)
        log_error("pipeline_s1", str(exc), platform="S1",
                   context={"product_id": str(product.id), "product_name": product.product_name})
        raise

log_error opens its own short-lived DB session so it works from anywhere
(Celery task, pipeline module, FastAPI route) without needing the caller's
session threaded through -- and it never raises, so a logging failure can
never mask or replace the real exception that triggered it.
"""
import logging
import traceback as tb_module

from vyom.db import SessionLocal
from vyom.models import ErrorLog

logger = logging.getLogger("vyom.error_log")


def log_error(source: str, message: str, *, platform: str | None = None,
              level: str = "error", context: dict | None = None,
              include_traceback: bool = True) -> None:
    """Write one row to error_logs. Never raises -- a broken logging call
    must never hide or replace the real exception being reported."""
    try:
        db = SessionLocal()
        try:
            row = ErrorLog(
                source=source,
                platform=platform,
                level=level,
                message=message[:4000],  # keep individual rows bounded
                traceback=tb_module.format_exc() if include_traceback else None,
                context=context or {},
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 -- logging must never itself raise
        logger.exception(
            "Failed to write to error_logs (message was: %s)", message)
