from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from vyom.auth import require_error_panel_access
from vyom.db import get_db
from vyom.models import ErrorLog

router = APIRouter(prefix="/errors", tags=["errors"],
                   dependencies=[Depends(require_error_panel_access)])


class ErrorLogOut(BaseModel):
    id: int
    source: str
    platform: Optional[str]
    level: str
    message: str
    traceback: Optional[str]
    context: dict
    resolved: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ErrorLogListOut(BaseModel):
    total: int
    unresolved_count: int
    items: list[ErrorLogOut]


@router.get("", response_model=ErrorLogListOut)
def list_errors(
    db: Session = Depends(get_db),
    source: Optional[str] = Query(
        None, description="Filter by source, e.g. 'pipeline_s1'"),
    platform: Optional[str] = Query(
        None, description="Filter by 'S1' or 'S2'"),
    resolved: Optional[bool] = Query(
        None, description="Filter by resolved state"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """One place to see every failure across discovery/download/processing/
    zonal-stats/API/auth, newest first -- this is what the dashboard's Errors
    panel calls."""
    q = select(ErrorLog)
    if source:
        q = q.where(ErrorLog.source == source)
    if platform:
        q = q.where(ErrorLog.platform == platform)
    if resolved is not None:
        q = q.where(ErrorLog.resolved == resolved)

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    unresolved_count = db.scalar(
        select(func.count()).select_from(
            ErrorLog).where(ErrorLog.resolved.is_(False))
    )

    rows = db.execute(
        q.order_by(ErrorLog.created_at.desc()).limit(limit).offset(offset)
    ).scalars().all()

    return ErrorLogListOut(total=total, unresolved_count=unresolved_count, items=rows)


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    """Distinct source values seen so far, for populating a filter dropdown
    in the panel without hardcoding the list on the frontend."""
    rows = db.execute(select(ErrorLog.source).distinct()).scalars().all()
    return {"sources": sorted(rows)}


@router.patch("/{error_id}/resolve", response_model=ErrorLogOut)
def resolve_error(error_id: int, db: Session = Depends(get_db)):
    row = db.get(ErrorLog, error_id)
    if row is None:
        raise HTTPException(404, "Error log not found")
    row.resolved = True
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/{error_id}/unresolve", response_model=ErrorLogOut)
def unresolve_error(error_id: int, db: Session = Depends(get_db)):
    row = db.get(ErrorLog, error_id)
    if row is None:
        raise HTTPException(404, "Error log not found")
    row.resolved = False
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
