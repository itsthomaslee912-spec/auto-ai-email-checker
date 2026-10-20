from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.config import get_settings
from app.models import MailboxJobState
from app.services.service_health import INSTANCE_ID


ACTIVE_STATES = {"queued", "starting", "running"}


def _lease_deadline(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(
        seconds=max(30, get_settings().mailbox_job_lease_seconds)
    )


def _recover_expired(db: Session, job_type: str, now: datetime) -> int:
    rows = db.query(MailboxJobState).filter(
        MailboxJobState.job_type == job_type,
        MailboxJobState.state.in_(("starting", "running")),
        or_(
            MailboxJobState.lease_expires_at.is_(None),
            MailboxJobState.lease_expires_at <= now,
        ),
    ).all()
    for row in rows:
        row.state = "queued"
        row.claimed_by = None
        row.lease_expires_at = None
        row.next_retry_at = now
        row.last_error = "Worker lease expired; job returned to the queue"
        row.updated_at = now
    return len(rows)


def default_status(mailbox_id: int, job_type: str) -> dict[str, Any]:
    common = {"mailbox_id": mailbox_id, "state": "idle", "failed": 0, "total": 0, "message": ""}
    if job_type == "sync":
        return {**common, "listed": 0, "skipped": 0, "imported": 0}
    return {**common, "updated": 0, "processed": 0}


def _decode(row: MailboxJobState) -> dict[str, Any]:
    status = default_status(row.mailbox_id, row.job_type)
    try:
        status.update(json.loads(row.progress or "{}"))
    except (TypeError, ValueError):
        pass
    status["mailbox_id"] = row.mailbox_id
    status["state"] = row.state
    return status


def get_status(db: Session, mailbox_id: int, job_type: str) -> dict[str, Any]:
    row = db.query(MailboxJobState).filter(
        MailboxJobState.mailbox_id == mailbox_id,
        MailboxJobState.job_type == job_type,
    ).one_or_none()
    return _decode(row) if row is not None else default_status(mailbox_id, job_type)


def enqueue(
    db: Session, mailbox_id: int, job_type: str, *, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    _recover_expired(db, job_type, now)
    row = db.query(MailboxJobState).filter(
        MailboxJobState.mailbox_id == mailbox_id,
        MailboxJobState.job_type == job_type,
    ).one_or_none()
    if row is not None and row.state in ACTIVE_STATES:
        db.commit()
        return _decode(row)
    status = default_status(mailbox_id, job_type)
    status.update(state="queued", message=f"Queued for the {job_type} service")
    if row is None:
        row = MailboxJobState(mailbox_id=mailbox_id, job_type=job_type)
        db.add(row)
    row.state = "queued"
    row.options = json.dumps(options or {}, separators=(",", ":"))
    row.progress = json.dumps(status, separators=(",", ":"))
    row.stop_requested = False
    row.claimed_by = None
    row.lease_expires_at = None
    row.next_retry_at = now
    row.last_error = None
    row.updated_at = now
    db.commit()
    return status


def persist(mailbox_id: int, job_type: str, status: dict[str, Any]) -> None:
    with SessionLocal() as db:
        row = db.query(MailboxJobState).filter(
            MailboxJobState.mailbox_id == mailbox_id,
            MailboxJobState.job_type == job_type,
        ).one_or_none()
        if row is None:
            row = MailboxJobState(mailbox_id=mailbox_id, job_type=job_type)
            db.add(row)
        row.state = str(status.get("state") or "idle")
        row.progress = json.dumps(status, default=str, separators=(",", ":"))
        row.updated_at = datetime.now(timezone.utc)
        if row.state in {"starting", "running"}:
            row.claimed_by = INSTANCE_ID
            row.lease_expires_at = _lease_deadline(row.updated_at)
        elif row.state not in ACTIVE_STATES:
            row.stop_requested = False
            row.claimed_by = None
            row.lease_expires_at = None
            row.next_retry_at = None
            row.last_error = str(status.get("message") or "")[:512] if row.state == "error" else None
        db.commit()


def request_stop(db: Session, mailbox_id: int, job_type: str) -> dict[str, Any]:
    row = db.query(MailboxJobState).filter(
        MailboxJobState.mailbox_id == mailbox_id,
        MailboxJobState.job_type == job_type,
    ).one_or_none()
    if row is None:
        return default_status(mailbox_id, job_type)
    row.stop_requested = True
    status = _decode(row)
    status["message"] = f"Stopping {job_type}..."
    row.progress = json.dumps(status, separators=(",", ":"))
    db.commit()
    return status


def stop_is_requested(mailbox_id: int, job_type: str) -> bool:
    with SessionLocal() as db:
        return bool(db.query(MailboxJobState.stop_requested).filter(
            MailboxJobState.mailbox_id == mailbox_id,
            MailboxJobState.job_type == job_type,
        ).scalar())


def claim(job_type: str, *, limit: int = 10) -> list[tuple[int, dict[str, Any]]]:
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        _recover_expired(db, job_type, now)
        stmt = (
            select(MailboxJobState)
            .where(
                MailboxJobState.job_type == job_type,
                MailboxJobState.state == "queued",
                or_(
                    MailboxJobState.next_retry_at.is_(None),
                    MailboxJobState.next_retry_at <= now,
                ),
            )
            .order_by(MailboxJobState.updated_at, MailboxJobState.id)
            .limit(limit)
        )
        if db.bind and db.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        rows = list(db.scalars(stmt))
        claimed: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            row.state = "starting"
            row.claimed_by = INSTANCE_ID
            row.lease_expires_at = _lease_deadline(now)
            row.attempts = int(row.attempts or 0) + 1
            row.last_error = None
            row.updated_at = now
            try:
                options = json.loads(row.options or "{}")
            except (TypeError, ValueError):
                options = {}
            claimed.append((row.mailbox_id, options))
        db.commit()
        return claimed
