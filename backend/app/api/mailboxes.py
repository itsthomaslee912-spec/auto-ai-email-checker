from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.oauth_google import ensure_google_access_token
from app.auth.oauth_microsoft import ensure_microsoft_access_token
from app.db import SessionLocal, get_db
from app.classify.outcome_extract import OUTCOME_LABELS, apply_outcome
from app.models import EmailLabel, EmailMessage, MailboxConnection, Provider
from app.realtime.gmail_watch import stop_gmail_watch
from app.realtime.outlook_subscriptions import (
    delete_outlook_subscriptions,
    parse_outlook_subscription_ids,
)
from app.realtime.sse import publish
from app.schemas import (
    LabelTimelineOut,
    LabelTimelineBucketOut,
    MailboxLabelStatsOut,
    MailboxOut,
    MailboxOutcomesOut,
    OutcomeEntryOut,
)
from app.services.mailbox_cleanup import delete_emails_for_mailbox

router = APIRouter(prefix="/api/mailboxes", tags=["mailboxes"])

TIMELINE_LABELS = frozenset(item.value for item in EmailLabel)
_HOUR_SPAN = timedelta(hours=36)


def _parse_range(date_from: str, date_to: str) -> tuple[datetime, datetime, str, str]:
    start, start_label = _parse_bound(date_from, is_end=False)
    end, end_label = _parse_bound(date_to, is_end=True)
    if start >= end:
        raise HTTPException(status_code=400, detail="date_from must be on or before date_to")
    return start, end, start_label, end_label


def _parse_bound(value: str, *, is_end: bool) -> tuple[datetime, str]:
    raw = value.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        day = date.fromisoformat(raw)
        start = datetime(day.year, day.month, day.day)
        if is_end:
            return start + timedelta(days=1), raw
        return start, raw
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        parsed = parsed.replace(tzinfo=None)
    if is_end:
        return parsed + timedelta(microseconds=1), parsed.isoformat()
    return parsed, parsed.isoformat()


def _active_mailbox(db: Session, mailbox_id: int) -> MailboxConnection:
    mailbox = (
        db.query(MailboxConnection)
        .filter(MailboxConnection.id == mailbox_id, MailboxConnection.is_active.is_(True))
        .one_or_none()
    )
    if mailbox is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    return mailbox


def _count_labels(db: Session, mailbox_ids: list[int], start: datetime, end: datetime) -> tuple[dict[str, int], int]:
    valid = {item.value for item in EmailLabel}
    label_counts = {item: 0 for item in valid}
    if not mailbox_ids:
        return label_counts, 0
    rows = (
        db.query(EmailMessage.label, func.count(EmailMessage.id))
        .filter(
            EmailMessage.mailbox_id.in_(mailbox_ids),
            EmailMessage.received_at.is_not(None),
            EmailMessage.received_at >= start,
            EmailMessage.received_at < end,
        )
        .group_by(EmailMessage.label)
        .all()
    )
    total = 0
    for lab, count in rows:
        key = lab if lab in valid else EmailLabel.OTHER.value
        n = int(count)
        label_counts[key] = label_counts.get(key, 0) + n
        total += n
    return label_counts, total


def _stats_out(
    mailbox_id: int | None,
    date_from: str,
    date_to: str,
    label_counts: dict[str, int],
    total: int,
) -> MailboxLabelStatsOut:
    return MailboxLabelStatsOut(
        mailbox_id=mailbox_id,
        date_from=date_from,
        date_to=date_to,
        total=total,
        label_counts=label_counts,
    )


@router.get("", response_model=list[MailboxOut])
def list_mailboxes(db: Session = Depends(get_db)) -> list[MailboxConnection]:
    return (
        db.query(MailboxConnection)
        .filter(MailboxConnection.is_active.is_(True))
        .order_by(MailboxConnection.created_at.desc())
        .all()
    )


@router.get("/label-stats", response_model=MailboxLabelStatsOut)
def all_mailboxes_label_stats(
    date_from: str = Query(...),
    date_to: str = Query(...),
    db: Session = Depends(get_db),
) -> MailboxLabelStatsOut:
    start, end, start_label, end_label = _parse_range(date_from, date_to)
    mailbox_ids = [
        row.id
        for row in db.query(MailboxConnection.id).filter(MailboxConnection.is_active.is_(True)).all()
    ]
    label_counts, total = _count_labels(db, mailbox_ids, start, end)
    return _stats_out(None, start_label, end_label, label_counts, total)


@router.get("/{mailbox_id}/label-stats", response_model=MailboxLabelStatsOut)
def mailbox_label_stats(
    mailbox_id: int,
    date_from: str = Query(...),
    date_to: str = Query(...),
    db: Session = Depends(get_db),
) -> MailboxLabelStatsOut:
    _active_mailbox(db, mailbox_id)
    start, end, start_label, end_label = _parse_range(date_from, date_to)
    label_counts, total = _count_labels(db, [mailbox_id], start, end)
    return _stats_out(mailbox_id, start_label, end_label, label_counts, total)


@router.get("/{mailbox_id}/label-timeline", response_model=LabelTimelineOut)
def mailbox_label_timeline(
    mailbox_id: int,
    label: str = Query(...),
    date_from: str = Query(...),
    date_to: str = Query(...),
    db: Session = Depends(get_db),
) -> LabelTimelineOut:
    if label not in TIMELINE_LABELS:
        raise HTTPException(status_code=400, detail="Unsupported timeline label")
    _active_mailbox(db, mailbox_id)
    start, end, start_label, end_label = _parse_range(date_from, date_to)
    hourly = (end - start) <= _HOUR_SPAN
    times = (
        db.query(EmailMessage.received_at)
        .filter(
            EmailMessage.mailbox_id == mailbox_id,
            EmailMessage.label == label,
            EmailMessage.received_at.is_not(None),
            EmailMessage.received_at >= start,
            EmailMessage.received_at < end,
        )
        .all()
    )
    counts: dict[datetime, int] = {}
    for (received_at,) in times:
        if received_at is None:
            continue
        stamp = received_at.astimezone(timezone.utc).replace(tzinfo=None) if received_at.tzinfo else received_at
        key = (
            stamp.replace(minute=0, second=0, microsecond=0)
            if hourly
            else datetime(stamp.year, stamp.month, stamp.day)
        )
        counts[key] = counts.get(key, 0) + 1
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    cursor = start.replace(minute=0, second=0, microsecond=0) if hourly else datetime(start.year, start.month, start.day)
    buckets: list[LabelTimelineBucketOut] = []
    while cursor < end:
        buckets.append(LabelTimelineBucketOut(bucket=cursor.isoformat(), count=counts.get(cursor, 0)))
        cursor += step
    return LabelTimelineOut(
        mailbox_id=mailbox_id,
        label=label,
        bucket="hour" if hourly else "day",
        date_from=start_label,
        date_to=end_label,
        buckets=buckets,
    )


def _collapse_outcomes(rows: list[EmailMessage]) -> dict[str, list[OutcomeEntryOut]]:
    grouped: dict[str, list[OutcomeEntryOut]] = {label: [] for label in OUTCOME_LABELS}
    seen: dict[str, dict[tuple[str, str], OutcomeEntryOut]] = {label: {} for label in OUTCOME_LABELS}
    blanks: dict[str, list[OutcomeEntryOut]] = {label: [] for label in OUTCOME_LABELS}
    for row in rows:
        label = row.label if row.label in OUTCOME_LABELS else ""
        if not label:
            continue
        company = (row.company or "").strip()
        role = (row.job_role or "").strip()
        entry = OutcomeEntryOut(
            company=company,
            role=role,
            received_at=row.received_at,
            subject=row.subject or "",
        )
        if not company and not role:
            blanks[label].append(entry)
            continue
        key = (company.casefold(), role.casefold())
        if key not in seen[label]:
            seen[label][key] = entry
    for label in OUTCOME_LABELS:
        grouped[label] = list(seen[label].values()) + blanks[label]
    return grouped


@router.get("/{mailbox_id}/outcomes", response_model=MailboxOutcomesOut)
def mailbox_outcomes(
    mailbox_id: int,
    date_from: str = Query(...),
    date_to: str = Query(...),
    label: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> MailboxOutcomesOut:
    if label is not None and label not in OUTCOME_LABELS:
        raise HTTPException(status_code=400, detail="Unsupported outcome label")
    _active_mailbox(db, mailbox_id)
    start, end, start_label, end_label = _parse_range(date_from, date_to)
    rows = (
        db.query(EmailMessage)
        .filter(
            EmailMessage.mailbox_id == mailbox_id,
            EmailMessage.label.in_(OUTCOME_LABELS),
            EmailMessage.received_at.is_not(None),
            EmailMessage.received_at >= start,
            EmailMessage.received_at < end,
        )
        .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
        .all()
    )
    grouped = _collapse_outcomes(rows)
    if label is None:
        return MailboxOutcomesOut(
            mailbox_id=mailbox_id,
            date_from=start_label,
            date_to=end_label,
            application_confirmation=grouped[EmailLabel.APPLICATION_CONFIRMATION.value],
            rejected_closed=grouped[EmailLabel.REJECTED_CLOSED.value],
            screening=grouped[EmailLabel.SCREENING.value],
            interview_scheduled=grouped[EmailLabel.INTERVIEW_SCHEDULED.value],
        )
    page = grouped[label]
    return MailboxOutcomesOut(
        mailbox_id=mailbox_id,
        date_from=start_label,
        date_to=end_label,
        items=page[offset : offset + limit],
        total=len(page),
        label=label,
    )


async def _extract_outcomes_for_label(mailbox_id: int, label: str) -> None:
    db = SessionLocal()
    try:
        rows = db.query(EmailMessage).filter(
            EmailMessage.mailbox_id == mailbox_id,
            EmailMessage.label == label,
        ).all()
        for row in rows:
            if await apply_outcome(db, row, force=True):
                db.commit()
    finally:
        db.close()


@router.post("/{mailbox_id}/outcomes/extract")
async def extract_mailbox_outcomes(
    mailbox_id: int,
    background_tasks: BackgroundTasks,
    label: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    if label not in {EmailLabel.REJECTED_CLOSED.value, EmailLabel.INTERVIEW_INVITATION.value}:
        raise HTTPException(status_code=400, detail="Choose Rejected / Closed or Interview Invitation")
    _active_mailbox(db, mailbox_id)
    background_tasks.add_task(_extract_outcomes_for_label, mailbox_id, label)
    return {"ok": True, "message": "AI extraction started in the background"}


@router.delete("/{mailbox_id}")
async def disconnect_mailbox(mailbox_id: int, db: Session = Depends(get_db)) -> dict:
    mailbox = db.query(MailboxConnection).filter(MailboxConnection.id == mailbox_id).one_or_none()
    if mailbox is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")

    try:
        if mailbox.provider == Provider.GOOGLE.value:
            token = await ensure_google_access_token(db, mailbox)
            await stop_gmail_watch(token)
        elif mailbox.provider == Provider.MICROSOFT.value and mailbox.webhook and mailbox.webhook.external_id:
            token = await ensure_microsoft_access_token(db, mailbox)
            await delete_outlook_subscriptions(
                token,
                parse_outlook_subscription_ids(mailbox.webhook.external_id),
            )
    except Exception:
        # Still disconnect locally even if remote cleanup fails.
        pass

    try:
        from app.services.mailbox_sync import request_stop_sync
        from app.services.reclassify import request_stop_reclassify

        await request_stop_sync(mailbox_id)
        await request_stop_reclassify(mailbox_id)
    except Exception:
        pass

    deleted_emails = delete_emails_for_mailbox(db, mailbox_id)
    mailbox.is_active = False
    db.commit()
    await publish("mailbox.disconnected", {"mailbox_id": mailbox_id, "deleted_emails": deleted_emails})
    return {"ok": True, "deleted_emails": deleted_emails}


@router.post("/{mailbox_id}/sync")
async def sync_mailbox(mailbox_id: int, db: Session = Depends(get_db)) -> dict:
    from app.services.mailbox_sync import start_mailbox_sync

    mailbox = (
        db.query(MailboxConnection)
        .filter(MailboxConnection.id == mailbox_id, MailboxConnection.is_active.is_(True))
        .one_or_none()
    )
    if mailbox is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    status = await start_mailbox_sync(mailbox.id, force_full=True)
    return {"ok": True, **status}


@router.post("/{mailbox_id}/sync/stop")
async def stop_mailbox_sync(mailbox_id: int, db: Session = Depends(get_db)) -> dict:
    from app.services.mailbox_sync import request_stop_sync

    mailbox = (
        db.query(MailboxConnection)
        .filter(MailboxConnection.id == mailbox_id, MailboxConnection.is_active.is_(True))
        .one_or_none()
    )
    if mailbox is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    status = await request_stop_sync(mailbox.id)
    return {"ok": True, **status}


@router.post("/{mailbox_id}/sync/full")
async def full_rescan_mailbox(mailbox_id: int, db: Session = Depends(get_db)) -> dict:
    from app.services.mailbox_sync import start_mailbox_sync

    mailbox = (
        db.query(MailboxConnection)
        .filter(MailboxConnection.id == mailbox_id, MailboxConnection.is_active.is_(True))
        .one_or_none()
    )
    if mailbox is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    status = await start_mailbox_sync(mailbox.id, force_full=True)
    return {"ok": True, **status}


@router.get("/{mailbox_id}/sync-status")
def mailbox_sync_status(mailbox_id: int) -> dict:
    from app.services.mailbox_sync import get_sync_status

    return get_sync_status(mailbox_id)


@router.post("/{mailbox_id}/reclassify")
async def reclassify_mailbox(mailbox_id: int, db: Session = Depends(get_db)) -> dict:
    from app.services.reclassify import start_mailbox_reclassify

    mailbox = (
        db.query(MailboxConnection)
        .filter(MailboxConnection.id == mailbox_id, MailboxConnection.is_active.is_(True))
        .one_or_none()
    )
    if mailbox is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    status = await start_mailbox_reclassify(mailbox.id)
    return {"ok": True, **status}


@router.post("/{mailbox_id}/reclassify/stop")
async def stop_mailbox_reclassify(mailbox_id: int, db: Session = Depends(get_db)) -> dict:
    from app.services.reclassify import request_stop_reclassify

    mailbox = (
        db.query(MailboxConnection)
        .filter(MailboxConnection.id == mailbox_id, MailboxConnection.is_active.is_(True))
        .one_or_none()
    )
    if mailbox is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    status = await request_stop_reclassify(mailbox.id)
    return {"ok": True, **status}


@router.get("/{mailbox_id}/reclassify-status")
def mailbox_reclassify_status(mailbox_id: int) -> dict:
    from app.services.reclassify import get_reclassify_status

    return get_reclassify_status(mailbox_id)
