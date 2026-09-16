"""
notifications.py -- the API behind the dashboard's notification bell.
Creation logic lives in vyom/notifications.py (called from tasks.py,
error_log.py, contact.py); this file is read/list/mark-read only.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from vyom.auth import require_auth, stable_owner_uuid
from vyom.db import get_db
from vyom.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str | None
    farm_id: uuid.UUID | None
    context: dict
    read: bool
    created_at: datetime

    class Config:
        from_attributes = True


def _to_out(row: Notification) -> NotificationOut:
    return NotificationOut(
        id=row.id, type=row.type, title=row.title, body=row.body,
        farm_id=row.farm_id, context=row.context or {},
        read=row.read_at is not None, created_at=row.created_at,
    )


@router.get("", response_model=list[NotificationOut])
def list_notifications(limit: int = 50, current_user: str = Depends(require_auth),
                       db: Session = Depends(get_db)):
    """Most recent notifications for the caller, newest first. limit is
    capped at 200 -- this is a bell-dropdown feed, not an export."""
    owner = stable_owner_uuid(current_user)
    limit = max(1, min(limit, 200))
    rows = db.execute(
        select(Notification)
        .where(Notification.user_id == owner)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/unread-count")
def unread_count(current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    """Powers the bell icon's badge -- polled periodically by the frontend
    rather than pushed, since there's no websocket/SSE layer in this app."""
    owner = stable_owner_uuid(current_user)
    count = db.execute(
        select(func.count(Notification.id)).where(
            Notification.user_id == owner, Notification.read_at.is_(None))
    ).scalar_one()
    return {"count": count}


@router.post("/{notification_id}/read")
def mark_read(notification_id: uuid.UUID, current_user: str = Depends(require_auth),
              db: Session = Depends(get_db)):
    owner = stable_owner_uuid(current_user)
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != owner:
        raise HTTPException(404, "Notification not found")
    if row.read_at is None:
        row.read_at = datetime.utcnow()
        db.add(row)
        db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    owner = stable_owner_uuid(current_user)
    now = datetime.utcnow()
    rows = db.execute(
        select(Notification).where(Notification.user_id ==
                                   owner, Notification.read_at.is_(None))
    ).scalars().all()
    for row in rows:
        row.read_at = now
        db.add(row)
    db.commit()
    return {"ok": True, "marked": len(rows)}
