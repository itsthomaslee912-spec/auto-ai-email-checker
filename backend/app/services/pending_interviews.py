"""Interview requests that have not been booked yet.

An invitation, or a reschedule that drops the old time without naming a new
one, stays pending until a later message for the same interview books it,
closes the application, or follows up after the interview.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import CalendarEvent, CalendarEventEmail, EmailMessage
from app.services.calendar_extract import companies_match, leaves_interview_unbooked, roles_compatible
from app.timeutil import as_utc

PENDING_WINDOW = timedelta(days=21)
_CLOSING_LABELS = {"interview_scheduled", "rejected_closed", "offer", "interview_follow_up"}
_IGNORED_FOLDERS = ["trash", "spam", "sent"]
_STAGE_WORDS_RE = re.compile(r"\b(screen|screening|interview|discussion|call|chat|intro|introduction|meeting|round)\b", re.IGNORECASE)


@dataclass
class PendingInterview:
    email: EmailMessage
    kind: str
    slots: list[CalendarEvent] = field(default_factory=list)


def _same_role(left: str | None, right: str | None) -> bool:
    """Scheduler mail often puts the interview stage ("Phone Screen") where the job title belongs."""
    if _STAGE_WORDS_RE.search(left or "") or _STAGE_WORDS_RE.search(right or ""):
        return True
    return roles_compatible(left, right)


def _same_interview(left: EmailMessage, right: EmailMessage) -> bool:
    if left.mailbox_id != right.mailbox_id:
        return False
    if left.thread_id and left.thread_id == right.thread_id:
        return True
    return bool(left.company) and companies_match(left.company, right.company) and _same_role(left.job_role, right.job_role)


def _booked_by_event(request: EmailMessage, events: list[CalendarEvent], linked_email_ids: set[int]) -> bool:
    if request.id in linked_email_ids:
        return True
    received = as_utc(request.received_at)
    for event in events:
        if event.source_email_id == request.id:
            return True
        if event.mailbox_id != request.mailbox_id or not request.company:
            continue
        booked_at = as_utc(event.source_received_at) or as_utc(event.created_at)
        if booked_at is None or received is None or booked_at <= received:
            continue
        if companies_match(event.company, request.company) and _same_role(event.job_role, request.job_role):
            return True
    return False


def _confirmed_bookings(db: Session, mailbox_ids: list[int]) -> tuple[list[CalendarEvent], set[int]]:
    booked_events = (
        db.query(CalendarEvent)
        .filter(
            CalendarEvent.mailbox_id.in_(mailbox_ids),
            CalendarEvent.kind == "confirmed",
            CalendarEvent.is_visible.is_(True),
        )
        .all()
    )
    booked_ids = [event.id for event in booked_events]
    linked_email_ids = {
        row.email_id
        for row in db.query(CalendarEventEmail.email_id).filter(CalendarEventEmail.event_id.in_(booked_ids)).all()
    } if booked_ids else set()
    return booked_events, linked_email_ids


def resolved_availability_ids(db: Session, events: list[CalendarEvent]) -> set[int]:
    """Offered times whose interview was booked, closed, or replaced by a newer offer."""
    offers = [event for event in events if event.kind == "availability" and event.source_email_id]
    if not offers:
        return set()
    mailbox_ids = sorted({event.mailbox_id for event in offers})
    requests = {
        message.id: message
        for message in db.query(EmailMessage).filter(EmailMessage.id.in_({event.source_email_id for event in offers})).all()
    }
    received_times = [as_utc(message.received_at) for message in requests.values() if message.received_at]
    if not received_times:
        return set()
    offer_source_ids = {
        row.source_email_id
        for row in db.query(CalendarEvent.source_email_id).filter(
            CalendarEvent.mailbox_id.in_(mailbox_ids),
            CalendarEvent.kind == "availability",
            CalendarEvent.is_visible.is_(True),
            CalendarEvent.source_email_id.isnot(None),
        ).all()
    }
    later_messages = (
        db.query(EmailMessage)
        .filter(
            EmailMessage.mailbox_id.in_(mailbox_ids),
            EmailMessage.received_at > min(received_times),
            EmailMessage.folder.notin_(_IGNORED_FOLDERS),
            or_(EmailMessage.label.in_(_CLOSING_LABELS), EmailMessage.label == "interview_invitation"),
        )
        .all()
    )
    # A newer request only replaces the old times when it offers times itself or drops the booking.
    followers = [
        message for message in later_messages
        if message.label != "interview_invitation" or message.id in offer_source_ids
    ]
    booked_events, linked_email_ids = _confirmed_bookings(db, mailbox_ids)

    resolved: set[int] = set()
    verdicts: dict[int, bool] = {}
    for event in offers:
        request = requests.get(event.source_email_id)
        if request is None:
            continue
        if request.id not in verdicts:
            received = as_utc(request.received_at)
            verdicts[request.id] = request.pending_dismissed_at is not None or _booked_by_event(request, booked_events, linked_email_ids) or any(
                follower.id != request.id
                and received is not None
                and (as_utc(follower.received_at) or received) > received
                and _same_interview(request, follower)
                for follower in followers
            )
        if verdicts[request.id]:
            resolved.add(event.id)
    return resolved


def pending_interview_requests(db: Session, mailbox_ids: list[int], now: datetime | None = None) -> list[PendingInterview]:
    if not mailbox_ids:
        return []
    now = now or datetime.now(timezone.utc)
    since = now - PENDING_WINDOW
    messages = (
        db.query(EmailMessage)
        .filter(
            EmailMessage.mailbox_id.in_(mailbox_ids),
            EmailMessage.received_at >= since,
            EmailMessage.folder.notin_(_IGNORED_FOLDERS),
            or_(EmailMessage.label.in_(_CLOSING_LABELS), EmailMessage.label == "interview_invitation"),
        )
        .order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())
        .all()
    )
    requests: list[EmailMessage] = []
    closers: list[EmailMessage] = []
    for message in messages:
        if message.label == "interview_invitation" or leaves_interview_unbooked(message):
            requests.append(message)
        else:
            closers.append(message)
    booked_events, linked_email_ids = _confirmed_bookings(db, mailbox_ids)

    pending: list[PendingInterview] = []
    newest_requests: list[EmailMessage] = []
    for request in requests:
        if any(_same_interview(newer, request) for newer in newest_requests):
            continue
        newest_requests.append(request)
        if request.pending_dismissed_at is not None:
            continue
        received = as_utc(request.received_at)
        if any(
            (as_utc(closer.received_at) or since) > received and _same_interview(request, closer)
            for closer in closers
        ):
            continue
        if _booked_by_event(request, booked_events, linked_email_ids):
            continue
        kind = "invitation" if request.label == "interview_invitation" else "reschedule"
        pending.append(PendingInterview(request, kind))

    if pending:
        ids = [item.email.id for item in pending]
        linked = {
            row.event_id: row.email_id
            for row in db.query(CalendarEventEmail).filter(CalendarEventEmail.email_id.in_(ids)).all()
        }
        by_email = {item.email.id: item for item in pending}
        by_thread = {(item.email.mailbox_id, item.email.thread_id): item for item in pending if item.email.thread_id}
        associations = [CalendarEvent.source_email_id.in_(ids), CalendarEvent.id.in_(list(linked))]
        thread_ids = [thread for _, thread in by_thread]
        if thread_ids:
            associations.append(CalendarEvent.source_thread_id.in_(thread_ids))
        slots = (
            db.query(CalendarEvent)
            .filter(
                CalendarEvent.mailbox_id.in_(mailbox_ids),
                CalendarEvent.kind == "availability",
                CalendarEvent.is_visible.is_(True),
                CalendarEvent.end_at > now,
                or_(*associations),
            )
            .order_by(CalendarEvent.start_at.asc())
            .all()
        )
        resolved = resolved_availability_ids(db, slots)
        for slot in slots:
            if slot.id in resolved:
                continue
            owner = (
                by_email.get(slot.source_email_id)
                or by_email.get(linked.get(slot.id))
                or by_thread.get((slot.mailbox_id, slot.source_thread_id))
            )
            if owner is not None and slot not in owner.slots:
                owner.slots.append(slot)
    return pending
