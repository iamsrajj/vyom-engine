"""Scheduled clean-up jobs that stop storage and compute costs from growing
silently. Scheduled in celery_app.py; routed to the 'discover' queue."""
import logging

from vyom.celery_app import celery_app
from vyom.db import SessionLocal
from vyom import farm_lifecycle

logger = logging.getLogger("vyom.maintenance")


@celery_app.task(name="vyom.maintenance.cleanup_stale_drafts")
def cleanup_stale_drafts() -> dict:
    db = SessionLocal()
    try:
        n = farm_lifecycle.purge_stale_drafts(db)
        logger.info("cleanup_stale_drafts: removed %d draft(s)", n)
        return {"drafts_deleted": n}
    finally:
        db.close()


@celery_app.task(name="vyom.maintenance.purge_orphan_products")
def purge_orphan_products() -> dict:
    db = SessionLocal()
    try:
        result = farm_lifecycle.purge_orphan_products(db)
        logger.info("purge_orphan_products: %s", result)
        return result
    finally:
        db.close()
