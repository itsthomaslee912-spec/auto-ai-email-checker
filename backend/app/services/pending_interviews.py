"""Interview requests that have not been booked yet.

An invitation, or a reschedule that drops the old time without naming a new
one, stays pending until a later message for the same interview books it,
closes the application, or follows up after the interview.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, load_only

from app.models import CalendarEvent, CalendarEventEmail, EmailMessage
from app.services.calendar_extract import companies_match, leaves_interview_unbooked, roles_compatible
from app.timeutil import as_utc

PENDING_WINDOW = timedelta(days=21)
_CLOSING_LABELS = {"interview_scheduled", "rejected_closed", "offer", "interview_follow_up"}
_IGNORED_FOLDERS = ["trash", "spam", "sent"]
_STAGE_WORDS_RE = re.compile(r"\b(screen|screening|interview|discussion|call|chat|intro|introduction|meeting|round)\b", re.IGNORECASE)
_UNBOOKED_SUBTYPES = {"reschedule", "time_change"}

# Pending / resolved logic only needs metadata. Loading body_html/body_text for
# thousands of rows dominates calendar open latency.
_EMAIL_META = load_only(
    EmailMessage.id,
    EmailMessage.mailbox_id,
    EmailMessage.thread_id,
    EmailMessage.subject,
    EmailMessage.sender,
    EmailMessage.received_at,
    EmailMessage.snippet,
    EmailMessage.label,
    EmailMessage.interview_subtype,
    EmailMessage.company,
    EmailMessage.job_role,
    EmailMessage.pending_dismissed_at,
    EmailMessage.folder,
)
_EMAIL_META_WITH_BODY = load_only(
    EmailMessage.id,
    EmailMessage.mailbox_id,
    EmailMessage.thread_id,
    EmailMessage.subject,
    EmailMessage.sender,
    EmailMessage.received_at,
    EmailMessage.snippet,
    EmailMessage.body_text,
    EmailMessage.body_html,
    EmailMessage.label,
    EmailMessage.interview_subtype,
    EmailMessage.company,
    EmailMessage.job_role,
    EmailMessage.pending_dismissed_at,
    EmailMessage.folder,
)


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


@dataclass(frozen=True)
class _BookingRef:
    id: int
    mailbox_id: int
    source_email_id: int | None
    source_thread_id: str | None
    company: str
    job_role: str
    source_received_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True)
class _MailRef:
    id: int
    mailbox_id: int
    thread_id: str | None
    received_at: datetime | None
    company: str | None
    job_role: str | None
    label: str
    subject: str
    interview_subtype: str | None
    pending_dismissed_at: datetime | None


_cache_lock = threading.Lock()
_cache_inflight: dict[tuple, threading.Event] = {}
_booking_cache: dict[tuple, tuple[list[_BookingRef], set[int]]] = {}
_follower_cache: dict[tuple, list[_MailRef]] = {}


def _cached(slot: dict, key: tuple, loader):
    """Share one database read across the events list and the pending-interview list."""
    with _cache_lock:
        hit = slot.get(key)
        if hit is not None:
            return hit
        waiter = _cache_inflight.get(key)
        if waiter is None:
            waiter = threading.Event()
            _cache_inflight[key] = waiter
            owner = True
        else:
            owner = False
    if not owner:
        waiter.wait(5)
        with _cache_lock:
            hit = slot.get(key)
            if hit is not None:
                return hit
    try:
        value = loader()
        with _cache_lock:
            slot[key] = value
            if len(slot) > 16:
                slot.pop(next(iter(slot)))
        return value
    finally:
        if owner:
            with _cache_lock:
                _cache_inflight.pop(key, None)
            waiter.set()


def _booked_by_event(request: EmailMessage, events: list[_BookingRef] | tuple[_BookingRef, ...], linked_email_ids: set[int]) -> bool:
    if request.id in linked_email_ids:
        return True
    received = as_utc(request.received_at)
    for event in events:
        if event.source_email_id == request.id:
            return True
        if event.mailbox_id != request.mailbox_id:
            continue
        # The accepted reply and resulting booking often share the original
        # invitation thread even when the classifier left company blank. That
        # thread identity is stronger than fuzzy company/role matching and
        # prevents a booked interview from remaining in "needs attention".
        if request.thread_id and event.source_thread_id == request.thread_id:
            return True
        if not request.company:
            continue
        booked_at = as_utc(event.source_received_at) or as_utc(event.created_at)
        if booked_at is None or received is None or booked_at <= received:
            continue
        if companies_match(event.company, request.company) and _same_role(event.job_role, request.job_role):
            return True
    return False


def invalidate_calendar_read_cache() -> None:
    """Drop shared calendar reads after a booking or interview changes."""
    with _cache_lock:
        _booking_cache.clear()
        _follower_cache.clear()


def _confirmed_bookings(db: Session, mailbox_ids: list[int]) -> tuple[list[_BookingRef], set[int]]:
    """Confirmed rows used only to decide whether an offer was booked.

    The match needs a handful of columns. Loading every event body made the
    calendar list wait on text it never reads.
    """
    key = (id(db.get_bind()), tuple(mailbox_ids))

    def load() -> tuple[list[_BookingRef], set[int]]:
        booked_events = [
            _BookingRef(
                id=row.id,
                mailbox_id=row.mailbox_id,
                source_email_id=row.source_email_id,
                source_thread_id=row.source_thread_id,
                company=row.company or "",
                job_role=row.job_role or "",
                source_received_at=row.source_received_at,
                created_at=row.created_at,
            )
            for row in db.query(
                CalendarEvent.id,
                CalendarEvent.mailbox_id,
                CalendarEvent.source_email_id,
                CalendarEvent.source_thread_id,
                CalendarEvent.company,
                CalendarEvent.job_role,
                CalendarEvent.source_received_at,
                CalendarEvent.created_at,
            ).filter(
                CalendarEvent.mailbox_id.in_(mailbox_ids),
                CalendarEvent.kind == "confirmed",
                CalendarEvent.is_visible.is_(True),
            ).all()
        ]
        booked_ids = [event.id for event in booked_events]
        linked_email_ids = {
            row.email_id
            for row in db.query(CalendarEventEmail.email_id).filter(CalendarEventEmail.event_id.in_(booked_ids)).all()
        } if booked_ids else set()
        return booked_events, linked_email_ids

    bookings, linked = _cached(_booking_cache, key, load)
    return bookings, set(linked)


def _emails_by_id(db: Session, ids: set[int], *, with_body: bool = False) -> dict[int, EmailMessage]:
    if not ids:
        return {}
    options = _EMAIL_META_WITH_BODY if with_body else _EMAIL_META
    return {
        message.id: message
        for message in db.query(EmailMessage).options(options).filter(EmailMessage.id.in_(ids)).all()
    }


def _follower_messages(db: Session, mailbox_ids: list[int], after: datetime) -> list[_MailRef]:
    """Interview mail newer than `after`. One read serves both calendar requests."""
    after = as_utc(after) or datetime.now(timezone.utc)
    coverage = min(after, datetime.now(timezone.utc) - PENDING_WINDOW).replace(minute=0, second=0, microsecond=0)
    key = (id(db.get_bind()), tuple(mailbox_ids), coverage)

    def load() -> list[_MailRef]:
        return [
            _MailRef(
                id=row.id,
                mailbox_id=row.mailbox_id,
                thread_id=row.thread_id,
                received_at=row.received_at,
                company=row.company,
                job_role=row.job_role,
                label=row.label,
                subject=row.subject or "",
                interview_subtype=row.interview_subtype,
                pending_dismissed_at=row.pending_dismissed_at,
            )
            for row in db.query(
                EmailMessage.id,
                EmailMessage.mailbox_id,
                EmailMessage.thread_id,
                EmailMessage.received_at,
                EmailMessage.company,
                EmailMessage.job_role,
                EmailMessage.label,
                EmailMessage.subject,
                EmailMessage.interview_subtype,
                EmailMessage.pending_dismissed_at,
            ).filter(
                EmailMessage.mailbox_id.in_(mailbox_ids),
                EmailMessage.received_at >= coverage,
                EmailMessage.folder.notin_(_IGNORED_FOLDERS),
                or_(EmailMessage.label.in_(_CLOSING_LABELS), EmailMessage.label == "interview_invitation"),
            ).all()
        ]

    return [
        message for message in _cached(_follower_cache, key, load)
        if (received := as_utc(message.received_at)) is not None and received > after
    ]


def _hydrate_unbooked_candidates(db: Session, messages: list[EmailMessage]) -> list[EmailMessage]:
    """Reschedule checks need message text; load bodies only for those few rows."""
    need = {message.id for message in messages if message.interview_subtype in _UNBOOKED_SUBTYPES}
    if not need:
        return messages
    full = _emails_by_id(db, need, with_body=True)
    return [full.get(message.id, message) for message in messages]


def resolved_availability_ids(db: Session, events: list[CalendarEvent]) -> set[int]:
    """Offered times whose interview was booked, closed, or replaced by a newer offer."""
    offers = [event for event in events if event.kind == "availability" and event.source_email_id]
    if not offers:
        return set()
    mailbox_ids = sorted({event.mailbox_id for event in offers})
    requests = _emails_by_id(db, {event.source_email_id for event in offers if event.source_email_id})
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
    earliest = min(received_times)
    later_messages = _follower_messages(db, mailbox_ids, earliest)
    # A newer request only replaces the old times when it offers times itself or drops the booking.
    followers = [
        message for message in later_messages
        if message.label != "interview_invitation" or message.id in offer_source_ids
    ]
    # Our Sent proposals stay labeled "other", but they are the source of newer availability.
    offer_sources = _emails_by_id(db, {source_id for source_id in offer_source_ids if source_id})
    for source in offer_sources.values():
        if source not in followers:
            followers.append(source)
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
    messages = _follower_messages(db, mailbox_ids, since - timedelta(seconds=1))
    messages.sort(key=lambda message: (as_utc(message.received_at) or since, message.id), reverse=True)
    unbooked_ids = {
        message.id
        for message in _emails_by_id(
            db,
            {message.id for message in messages if message.interview_subtype in _UNBOOKED_SUBTYPES},
            with_body=True,
        ).values()
        if leaves_interview_unbooked(message)
    }
    requests: list[_MailRef] = []
    closers_by_mailbox: dict[int, list[_MailRef]] = {}
    for message in messages:
        if message.label == "interview_invitation":
            requests.append(message)
        elif message.id in unbooked_ids:
            requests.append(message)
        else:
            closers_by_mailbox.setdefault(message.mailbox_id, []).append(message)
    booked_events, linked_email_ids = _confirmed_bookings(db, mailbox_ids)
    booked_by_mailbox: dict[int, list[_BookingRef]] = {}
    for event in booked_events:
        booked_by_mailbox.setdefault(event.mailbox_id, []).append(event)

    pending: list[PendingInterview] = []
    newest_by_mailbox: dict[int, list[_MailRef]] = {}
    for request in requests:
        newest = newest_by_mailbox.setdefault(request.mailbox_id, [])
        if any(_same_interview(newer, request) for newer in newest):
            continue
        newest.append(request)
        if request.pending_dismissed_at is not None:
            continue
        received = as_utc(request.received_at)
        if any(
            (as_utc(closer.received_at) or since) > received and _same_interview(request, closer)
            for closer in closers_by_mailbox.get(request.mailbox_id, ())
        ):
            continue
        if _booked_by_event(request, booked_by_mailbox.get(request.mailbox_id, ()), linked_email_ids):
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
        # Include all recent availability for these mailboxes. Sent proposals use
        # their own source_email_id; company/role matching attaches them below.
        slots = (
            db.query(CalendarEvent)
            .filter(
                CalendarEvent.mailbox_id.in_(mailbox_ids),
                CalendarEvent.kind == "availability",
                CalendarEvent.is_visible.is_(True),
                # Keep recently offered times on the alert even after that day passes.
                CalendarEvent.start_at >= since,
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
            if owner is None and slot.company:
                # Sent proposals often keep their own source_email_id; match the
                # open invitation for the same company/role when thread ids differ.
                slot_at = as_utc(slot.source_received_at) or as_utc(slot.created_at)
                for item in pending:
                    if item.email.mailbox_id != slot.mailbox_id:
                        continue
                    received = as_utc(item.email.received_at)
                    if slot_at is None or received is None or slot_at < received:
                        continue
                    if companies_match(item.email.company, slot.company) and _same_role(
                        item.email.job_role, slot.job_role
                    ):
                        owner = item
                        break
            if owner is not None and slot not in owner.slots:
                owner.slots.append(slot)
    return pending
