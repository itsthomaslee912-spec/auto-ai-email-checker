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


def test_backfill_loop_does_not_refetch_mail_process_calendar_email_no_ops_on(monkeypatch):
    """process_calendar_email legitimately returns False without touching
    calendar_processed_hash for mail that only matched the batch query's
    "subject mentions interview" fallback but isn't actually a misfiled
    booking (e.g. a cold recruiter email with "Interview Process" in a
    footer). Confirmed live: this stranded the whole loop — it kept
    re-fetching the same stuck subset every batch, net zero progress,
    even once the real network-call bug above was also fixed."""
    factory, mailbox_id = _database()
    with factory() as db:
        db.add(EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="msg-not-a-booking",
            subject="Our Interview Process at Acme",
            sender="recruiter@example.com",
            body_text="We're excited you applied! Here's an overview of our interview process...",
            label=EmailLabel.OTHER.value,
            human_corrected=False,
        ))
        db.commit()

    monkeypatch.setattr(calendar_backfill, "SessionLocal", factory)
    monkeypatch.setattr(calendar_backfill, "BACKFILL_IDLE_SECONDS", 0)

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
        email = db.query(EmailMessage).filter(EmailMessage.provider_message_id == "msg-not-a-booking").one()
        assert email.calendar_processed_hash is not None
        assert email.label == EmailLabel.OTHER.value


def test_backfill_loop_skips_real_landing_page_http_calls(monkeypatch):
    """A Paradox/Teamtailor scheduling link would normally cost a real HTTP
    round trip (up to ~8s, several candidate URLs) per message — left on, a
    handful of these in one batch stalls the whole loop with no error and no
    visible progress. The bulk pass must skip it entirely."""
    factory, mailbox_id = _database()
    with factory() as db:
        db.add(EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="msg-paradox",
            subject="Interview scheduling",
            sender="recruiter@example.com",
            body_text=(
                _ics_body(datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc))
                + "\nhttps://olivia.paradox.ai/abcdef123456"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
        ))
        db.commit()

    monkeypatch.setattr(calendar_backfill, "SessionLocal", factory)
    monkeypatch.setattr(calendar_backfill, "BACKFILL_IDLE_SECONDS", 0)

    from app.services import calendar_extract

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("httpx should never be reached — the skip flag was not honored")

    real_client = calendar_extract.httpx.AsyncClient
    monkeypatch.setattr(calendar_extract.httpx, "AsyncClient", lambda **_kw: type(
        "FailingClient", (), {
            "__aenter__": fail_if_called,
            "__aexit__": fail_if_called,
        },
    )())

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
    monkeypatch.setattr(calendar_extract.httpx, "AsyncClient", real_client)

    with factory() as db:
        email = db.query(EmailMessage).filter(EmailMessage.provider_message_id == "msg-paradox").one()
        assert email.calendar_processed_hash is not None


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


def test_backfill_failure_is_recorded_without_starving_later_mail(monkeypatch):
    factory, mailbox_id = _database()
    with factory() as db:
        db.add_all([
            EmailMessage(
                mailbox_id=mailbox_id,
                provider_message_id="broken",
                subject="Interview broken",
                sender="recruiter@example.com",
                label=EmailLabel.INTERVIEW_SCHEDULED.value,
                received_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            ),
            EmailMessage(
                mailbox_id=mailbox_id,
                provider_message_id="later",
                subject="Interview later",
                sender="recruiter@example.com",
                label=EmailLabel.INTERVIEW_SCHEDULED.value,
                received_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
            ),
        ])
        db.commit()

    monkeypatch.setattr(calendar_backfill, "SessionLocal", factory)
    monkeypatch.setattr(calendar_backfill, "BACKFILL_IDLE_SECONDS", 0)

    async def process(_db, email, **_kwargs):
        if email.provider_message_id == "broken":
            raise ValueError("malformed calendar payload")
        email.calendar_processed_hash = calendar_backfill.email_calendar_hash(email)
        email.calendar_processed_at = datetime.now(timezone.utc)
        return False

    monkeypatch.setattr(calendar_backfill, "process_calendar_email", process)

    async def run():
        stop_event = threading.Event()
        task = asyncio.ensure_future(
            calendar_backfill._backfill_calendar_events(stop_event, asyncio.get_event_loop())
        )
        for _ in range(50):
            await asyncio.sleep(0.02)
            with factory() as db:
                remaining = db.query(EmailMessage).filter(
                    EmailMessage.calendar_processed_hash.is_(None)
                ).count()
            if remaining == 0:
                break
        stop_event.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())

    with factory() as db:
        rows = {
            row.provider_message_id: row
            for row in db.query(EmailMessage).order_by(EmailMessage.id).all()
        }
        assert rows["broken"].calendar_processed_hash is not None
        assert rows["broken"].calendar_processing_error
        assert rows["later"].calendar_processed_hash is not None
