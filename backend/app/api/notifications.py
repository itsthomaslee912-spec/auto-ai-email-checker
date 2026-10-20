from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.auth.session import require_user
from app.db import get_db
from app.models import Notification

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _notification_out(row: Notification) -> dict:
    return {
        "id": row.notification_id,
        "source_event_id": row.source_event_id,
        "event_type": row.event_type,
        "channel": row.channel,
        "data": json.loads(row.payload),
        "is_read": row.is_read,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "read_at": row.read_at.isoformat() if row.read_at else None,
    }


@router.get("")
def list_notifications(
    unread_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(Notification).filter(Notification.user_id == user_id)
    if unread_only:
        query = query.filter(Notification.is_read.is_(False))
    rows = query.order_by(Notification.id.desc()).limit(limit).all()
    unread_count = db.query(Notification.id).filter(
        Notification.user_id == user_id,
        Notification.is_read.is_(False),
    ).count()
    return {
        "items": [_notification_out(row) for row in rows],
        "unread_count": unread_count,
    }


@router.post("/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.query(Notification).filter(
        Notification.notification_id == notification_id,
        Notification.user_id == user_id,
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not row.is_read:
        row.is_read = True
        row.read_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
    return _notification_out(row)


@router.post("/read-all")
def mark_all_notifications_read(
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        .values(is_read=True, read_at=now)
    )
    db.commit()
    return {"marked": int(result.rowcount or 0)}
