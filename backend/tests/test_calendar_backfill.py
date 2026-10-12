from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import EmailLabel, EmailMessage, MailboxConnection, Provider, User
from app.services import calendar_backfill


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with factory() as db:
        user = User(external_id="backfill-user")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id, provider=Provider.GOOGLE.value, email_address="a@example.com",
            access_token_enc="x", refresh_token_enc="", is_active=True,
        )
        db.add(mailbox)
        db.commit()
        mailbox_id = mailbox.id
    return factory, mailbox_id


def _ics_body(start: datetime) -> str:
    stamp = start.strftime("%Y%m%dT%H%M%SZ")
    return f"""BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
DTSTART:{stamp}
DTEND:{stamp}
END:VEVENT
END:VCALENDAR"""


def test_backfill_loop_processes_batch_then_idles_without_ai(monkeypatch):
    """A continuous pass should clear a small backlog using the fast,
    deterministic path (no AI call) and then idle instead of exiting."""
    factory, mailbox_id = _database()
    with factory() as db:
        for i in range(3):
            db.add(EmailMessage(
                mailbox_id=mailbox_id,
                provider_message_id=f"msg-{i}",
                subject=f"Interview {i}",
                sender="recruiter@example.com",
                body_text=_ics_body(datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)),
                label=EmailLabel.INTERVIEW_SCHEDULED.value,
            ))
        db.commit()

    monkeypatch.setattr(calendar_backfill, "SessionLocal", factory)
    monkeypatch.setattr(calendar_backfill, "BACKFILL_IDLE_SECONDS", 0)

    ai_calls: list[int] = []
    from app.services import calendar_extract
    real_extract = calendar_extract.extract_calendar_details

    async def spy_extract(email, related_emails=None, *, use_ai=True, known_slots=None):
        if use_ai:
            ai_calls.append(email.id)
        return await real_extract(email, related_emails, use_ai=use_ai, known_slots=known_slots)

    monkeypatch.setattr(calendar_extract, "extract_calendar_details", spy_extract)

    async def run():
        stop_event = threading.Event()
        task = asyncio.ensure_future(
            calendar_backfill._backfill_calendar_events(stop_event, asyncio.get_event_loop())
        )
        for _ in range(50):
            await asyncio.sleep(0.02)
            with factory() as db:
                remaining = (
                    db.query(EmailMessage)
                    .filter(EmailMessage.calendar_processed_hash.is_(None))
                    .count()
                )
            if remaining == 0:
                break
        stop_event.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())

    with factory() as db:
        processed = db.query(EmailMessage).filter(EmailMessage.calendar_processed_hash.isnot(None)).count()
    assert processed == 3
    assert ai_calls == []


def test_backfill_loop_exits_promptly_on_stop_event(monkeypatch):
    factory, _mailbox_id = _database()
    monkeypatch.setattr(calendar_backfill, "SessionLocal", factory)
    monkeypatch.setattr(calendar_backfill, "BACKFILL_IDLE_SECONDS", 5)

    async def run():
        stop_event = threading.Event()
        task = asyncio.ensure_future(
            calendar_backfill._backfill_calendar_events(stop_event, asyncio.get_event_loop())
        )
        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())
