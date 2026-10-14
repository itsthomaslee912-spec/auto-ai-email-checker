from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, contains_eager, defer

from app.auth.session import mailbox_scope
from app.db import get_db
from app.models import CalendarEvent, CalendarEventEmail, EmailMessage, MailboxConnection
from app.realtime.sse import publish
from app.schemas import (
    CalendarEventCreateIn,
    CalendarEventOut,
    CalendarEventUpdateIn,
    EmailPreviewOut,
    PendingInterviewOut,
    PendingInterviewSlotOut,
)
from app.services.pending_interviews import pending_interview_requests, resolved_availability_ids
from app.services.calendar_extract import clean_meeting_provider
from app.timeutil import as_utc

router = APIRouter(prefix="/api/calendar-events", tags=["calendar"])


def _active_mailbox(db: Session, mailbox_id: int) -> MailboxConnection:
    mailbox = db.query(MailboxConnection).filter(
        MailboxConnection.id == mailbox_id,
        mailbox_scope(),
    ).one_or_none()
    if mailbox is None:
        raise HTTPException(status_code=404, detail="Email account not found")
    return mailbox


def _event_query(db: Session):
    return (
        db.query(CalendarEvent)
        .join(MailboxConnection, MailboxConnection.id == CalendarEvent.mailbox_id)
        .options(contains_eager(CalendarEvent.mailbox))
        .filter(mailbox_scope())
    )


_RESCHEDULE_SUBTYPES = {"reschedule", "time_change"}


def _request_kinds(db: Session, events: list[CalendarEvent]) -> dict[int, str]:
    """Whether each unbooked availability item came from an invitation or a reschedule."""
    sources = {event.source_email_id for event in events if event.kind == "availability" and event.source_email_id}
    if not sources:
        return {}
    subtypes = dict(db.query(EmailMessage.id, EmailMessage.interview_subtype).filter(EmailMessage.id.in_(sources)).all())
    return {
        event.id: "reschedule" if subtypes.get(event.source_email_id) in _RESCHEDULE_SUBTYPES else "invitation"
        for event in events
        if event.kind == "availability"
    }


def _event_out(event: CalendarEvent, mailbox: MailboxConnection | None = None, request_kind: str | None = None) -> CalendarEventOut:
    box = mailbox or event.mailbox
    return CalendarEventOut(
        id=event.id,
        mailbox_id=event.mailbox_id,
        email_account=box.email_address,
        provider=box.provider,
        source_email_id=event.source_email_id,
        kind=event.kind,
        origin=event.origin,
        title=event.title,
        company=event.company or "",
        role=event.job_role or "",
        interviewer=event.interviewer or "",
        work_model=event.work_model or "unspecified",
        job_url=event.job_url,
        description=event.description or "",
        start_at=event.start_at,
        end_at=event.end_at,
        source_timezone=event.source_timezone,
        interview_status=event.interview_status,
        application_status=event.application_status or "active",
        meeting_type=event.meeting_type or "unspecified",
        # Never expose old AI placeholder text as a video-platform name.
        meeting_provider=clean_meeting_provider(event.meeting_provider),
        meeting_url=event.meeting_url,
        phone_number=event.phone_number,
        phone_access_code=event.phone_access_code,
        request_kind=request_kind,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def _validate_url(value: str | None, *, meeting: bool = False) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    allowed = {"https"} if meeting else {"http", "https"}
    if parsed.scheme.lower() not in allowed or not parsed.netloc:
        label = "Meeting link" if meeting else "Job link"
        raise HTTPException(status_code=422, detail=f"{label} must be a valid {'HTTPS' if meeting else 'HTTP(S)'} URL")
    return value


def _validate_phone(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not re.fullmatch(r"[+()\-\.\s\d]{7,64}", value):
        raise HTTPException(status_code=422, detail="Phone number contains unsupported characters")
    return value


def _validate_times(start_at: datetime, end_at: datetime) -> tuple[datetime, datetime]:
    start = as_utc(start_at)
    end = as_utc(end_at)
    if start is None or end is None or end <= start:
        raise HTTPException(status_code=422, detail="End time must be after start time")
    return start, end


@router.get("", response_model=list[CalendarEventOut])
def list_calendar_events(
    start: datetime = Query(...),
    end: datetime = Query(...),
    db: Session = Depends(get_db),
) -> list[CalendarEventOut]:
    range_start, range_end = _validate_times(start, end)
    rows = (
        _event_query(db)
        .filter(
            CalendarEvent.is_visible.is_(True),
            CalendarEvent.start_at < range_end,
            CalendarEvent.end_at > range_start,
        )
        .order_by(CalendarEvent.start_at.asc(), CalendarEvent.id.asc())
        .all()
    )
    resolved = resolved_availability_ids(db, rows)
    rows = [row for row in rows if row.id not in resolved]
    kinds = _request_kinds(db, rows)
    return [_event_out(row, request_kind=kinds.get(row.id)) for row in rows]


@router.get("/pending", response_model=list[PendingInterviewOut])
def list_pending_interviews(db: Session = Depends(get_db)) -> list[PendingInterviewOut]:
    mailboxes = {box.id: box for box in db.query(MailboxConnection).filter(mailbox_scope()).all()}
    items = pending_interview_requests(db, list(mailboxes))
    return [
        PendingInterviewOut(
            email_id=item.email.id,
            mailbox_id=item.email.mailbox_id,
            email_account=mailboxes[item.email.mailbox_id].email_address,
            kind=item.kind,
            company=item.email.company or "",
            role=item.email.job_role or "",
            subject=item.email.subject or "",
            received_at=item.email.received_at,
            slots=[PendingInterviewSlotOut(event_id=slot.id, start_at=slot.start_at, end_at=slot.end_at) for slot in item.slots],
        )
        for item in items
    ]


async def _set_pending_dismissed(db: Session, email_id: int, dismissed: bool) -> None:
    email = (
        db.query(EmailMessage)
        .join(MailboxConnection, MailboxConnection.id == EmailMessage.mailbox_id)
        .filter(EmailMessage.id == email_id, mailbox_scope())
        .one_or_none()
    )
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    email.pending_dismissed_at = datetime.now(timezone.utc) if dismissed else None
    db.commit()
    await publish("calendar.changed", {"action": "pending_dismissed" if dismissed else "pending_restored", "email_id": email_id, "mailbox_id": email.mailbox_id})


@router.post("/pending/{email_id}/dismiss", status_code=204)
async def dismiss_pending_interview(email_id: int, db: Session = Depends(get_db)) -> None:
    """Hide an unbooked invitation or reschedule and its offered times."""
    await _set_pending_dismissed(db, email_id, True)


@router.delete("/pending/{email_id}/dismiss", status_code=204)
async def restore_pending_interview(email_id: int, db: Session = Depends(get_db)) -> None:
    await _set_pending_dismissed(db, email_id, False)


@router.get("/{event_id}", response_model=CalendarEventOut)
def get_calendar_event(event_id: int, db: Session = Depends(get_db)) -> CalendarEventOut:
    event = _event_query(db).filter(CalendarEvent.id == event_id).one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Calendar event not found")
    return _event_out(event, request_kind=_request_kinds(db, [event]).get(event.id))


@router.get("/{event_id}/emails", response_model=list[EmailPreviewOut])
def list_calendar_event_emails(event_id: int, db: Session = Depends(get_db)) -> list[EmailMessage]:
    """Return every stored email linked to an event, newest first."""
    event = _event_query(db).filter(CalendarEvent.id == event_id).one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Calendar event not found")

    # Include legacy availability rows that were folded into this confirmed
    # event. This recovers invitations created before durable links existed.
    superseded_events = (
        db.query(CalendarEvent)
        .filter(
            CalendarEvent.mailbox_id == event.mailbox_id,
            CalendarEvent.superseded_by_event_id == event.id,
        )
        .all()
    )
    related_events = [event, *superseded_events]
    related_event_ids = {item.id for item in related_events}
    linked_ids = {
        email_id
        for item in related_events
        for email_id in (item.source_email_id, item.outcome_source_email_id)
        if email_id is not None
    }
    linked_ids.update(
        row[0]
        for row in db.query(CalendarEventEmail.email_id)
        .filter(CalendarEventEmail.event_id.in_(related_event_ids))
        .all()
    )

    thread_ids = {item.source_thread_id for item in related_events if item.source_thread_id}
    if linked_ids:
        thread_ids.update(
            row[0]
            for row in db.query(EmailMessage.thread_id)
            .filter(
                EmailMessage.mailbox_id == event.mailbox_id,
                EmailMessage.id.in_(linked_ids),
                EmailMessage.thread_id.is_not(None),
            )
            .all()
            if row[0]
        )

    associations = []
    if thread_ids:
        associations.append(EmailMessage.thread_id.in_(thread_ids))
    if linked_ids:
        associations.append(EmailMessage.id.in_(linked_ids))
    if not associations:
        return []

    return (
        db.query(EmailMessage)
        .options(defer(EmailMessage.body_html))
        .filter(
            EmailMessage.mailbox_id == event.mailbox_id,
            or_(*associations),
        )
        .order_by(
            func.coalesce(EmailMessage.received_at, EmailMessage.created_at).desc(),
            EmailMessage.id.desc(),
        )
        .all()
    )


@router.post("", response_model=CalendarEventOut, status_code=201)
async def create_calendar_event(
    payload: CalendarEventCreateIn,
    db: Session = Depends(get_db),
) -> CalendarEventOut:
    mailbox = _active_mailbox(db, payload.mailbox_id)
    start_at, end_at = _validate_times(payload.start_at, payload.end_at)
    event = CalendarEvent(
        mailbox_id=mailbox.id,
        kind="confirmed",
        origin="manual",
        title=payload.title.strip(),
        company=payload.company.strip(),
        job_role=payload.role.strip(),
        interviewer=payload.interviewer.strip(),
        work_model=payload.work_model,
        job_url=_validate_url(payload.job_url),
        description=payload.description.strip(),
        start_at=start_at,
        end_at=end_at,
        source_timezone=(payload.source_timezone or "").strip() or None,
        interview_status=payload.interview_status,
        application_status=payload.application_status,
        meeting_type=payload.meeting_type,
        meeting_provider=clean_meeting_provider(payload.meeting_provider),
        meeting_url=_validate_url(payload.meeting_url, meeting=True),
        phone_number=_validate_phone(payload.phone_number),
        phone_access_code=(payload.phone_access_code or "").strip() or None,
        is_visible=True,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    await publish("calendar.changed", {"action": "created", "event_id": event.id, "mailbox_id": event.mailbox_id})
    return _event_out(event, mailbox)


@router.patch("/{event_id}", response_model=CalendarEventOut)
async def update_calendar_event(
    event_id: int,
    payload: CalendarEventUpdateIn,
    db: Session = Depends(get_db),
) -> CalendarEventOut:
    event = _event_query(db).filter(CalendarEvent.id == event_id).one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Calendar event not found")
    fields = payload.model_fields_set
    if "title" in fields and payload.title is not None:
        event.title = payload.title.strip()
    if "company" in fields:
        event.company = (payload.company or "").strip()
    if "role" in fields:
        event.job_role = (payload.role or "").strip()
    if "interviewer" in fields:
        event.interviewer = (payload.interviewer or "").strip()
    if "work_model" in fields and payload.work_model is not None:
        event.work_model = payload.work_model
    if "job_url" in fields:
        event.job_url = _validate_url(payload.job_url)
    if "description" in fields:
        event.description = (payload.description or "").strip()
    if "source_timezone" in fields:
        event.source_timezone = (payload.source_timezone or "").strip() or None
    if "interview_status" in fields and payload.interview_status is not None:
        event.interview_status = payload.interview_status
    if "application_status" in fields and payload.application_status is not None:
        event.application_status = payload.application_status
    if "meeting_type" in fields and payload.meeting_type is not None:
        event.meeting_type = payload.meeting_type
    if "meeting_provider" in fields:
        event.meeting_provider = clean_meeting_provider(payload.meeting_provider)
    if "meeting_url" in fields:
        event.meeting_url = _validate_url(payload.meeting_url, meeting=True)
    if "phone_number" in fields:
        event.phone_number = _validate_phone(payload.phone_number)
    if "phone_access_code" in fields:
        event.phone_access_code = (payload.phone_access_code or "").strip() or None
    start_at = payload.start_at if "start_at" in fields and payload.start_at is not None else event.start_at
    end_at = payload.end_at if "end_at" in fields and payload.end_at is not None else event.end_at
    event.start_at, event.end_at = _validate_times(start_at, end_at)
    db.commit()
    db.refresh(event)
    await publish("calendar.changed", {"action": "updated", "event_id": event.id, "mailbox_id": event.mailbox_id})
    return _event_out(event)
