from __future__ import annotations

import asyncio
import logging
import threading

from sqlalchemy import and_, or_

from app.db import SessionLocal
from app.models import EmailMessage
from app.realtime.sse import publish
from app.services.calendar_extract import CALENDAR_LABELS, email_calendar_hash, needs_calendar_event_repair, process_calendar_email, refresh_calendar_event_details, repair_meeting_landing_urls

logger = logging.getLogger(__name__)


async def backfill_calendar_events(stop_event: asyncio.Event) -> None:
    """Repair stored calendar mail without freezing the API.

    The repair holds database locks and does synchronous work. Running it on
    the request loop makes health checks, webhooks, and the calendar wait
    behind it, so the work runs on its own thread and loop.
    """
    stop_flag = threading.Event()
    main_loop = asyncio.get_running_loop()

    async def mirror_stop() -> None:
        await stop_event.wait()
        stop_flag.set()

    mirror = asyncio.create_task(mirror_stop())
    try:
        await asyncio.to_thread(_run_calendar_backfill, stop_flag, main_loop)
    finally:
        mirror.cancel()


def _run_calendar_backfill(stop_flag: threading.Event, main_loop: asyncio.AbstractEventLoop) -> None:
    asyncio.run(_backfill_calendar_events(stop_flag, main_loop))


def _publish_from_backfill(
    main_loop: asyncio.AbstractEventLoop, event: str, data: dict,
) -> None:
    """Queue an update on the API loop without waiting for it.

    Waiting would deadlock on shutdown: the API loop is blocked until this
    thread finishes, and this thread would be blocked until the API loop
    publishes.
    """
    def _send() -> None:
        asyncio.create_task(publish(event, data))

    try:
        main_loop.call_soon_threadsafe(_send)
    except RuntimeError:
        logger.debug("Skipped calendar backfill event; API loop is closed")


async def _backfill_calendar_events(
    stop_event: threading.Event, main_loop: asyncio.AbstractEventLoop,
) -> None:
    def load_email_ids() -> list[int]:
        lookup = SessionLocal()
        try:
            return [
                row[0]
                for row in lookup.query(EmailMessage.id)
                .filter(
                    or_(
                        EmailMessage.label.in_(CALENDAR_LABELS),
                        # Booked interviews misfiled under another category.
                        and_(EmailMessage.subject.ilike("%interview%"), EmailMessage.human_corrected.is_(False)),
                    )
                )
                .order_by(EmailMessage.received_at.asc(), EmailMessage.id.asc())
                .all()
            ]
        finally:
            lookup.close()

    email_ids = await asyncio.to_thread(load_email_ids)
    db = SessionLocal()
    pending_mailboxes: set[int] = set()

    def publish_pending() -> None:
        for mailbox_id in list(pending_mailboxes):
            _publish_from_backfill(
                main_loop, "calendar.changed", {"action": "backfilled", "mailbox_id": mailbox_id},
            )
        pending_mailboxes.clear()

    try:
        for index, email_id in enumerate(email_ids):
            email = db.get(EmailMessage, email_id)
            if stop_event.is_set():
                break
            if email is None:
                continue
            try:
                if email.calendar_processed_hash == email_calendar_hash(email):
                    if needs_calendar_event_repair(db, email):
                        if await process_calendar_email(db, email, force=True, use_ai=False):
                            pending_mailboxes.add(email.mailbox_id)
                        db.commit()
                        continue
                    if refresh_calendar_event_details(db, email):
                        pending_mailboxes.add(email.mailbox_id)
                        db.commit()
                    continue
                if await process_calendar_email(db, email):
                    pending_mailboxes.add(email.mailbox_id)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Calendar backfill failed email_id=%s", email.id)
            if index % 5 == 0:
                publish_pending()
                await asyncio.sleep(0)
        if not stop_event.is_set():
            repaired_mailboxes = await repair_meeting_landing_urls(db)
            if repaired_mailboxes:
                pending_mailboxes.update(repaired_mailboxes)
                db.commit()
    finally:
        db.close()
    publish_pending()
