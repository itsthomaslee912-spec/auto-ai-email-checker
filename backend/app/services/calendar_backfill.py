from __future__ import annotations

import asyncio
import logging
import threading

from sqlalchemy import and_, or_

from app.db import SessionLocal
from app.models import EmailMessage
from app.realtime.sse import publish
from app.services.calendar_extract import CALENDAR_LABELS, email_calendar_hash, needs_calendar_event_repair, process_calendar_email, refresh_calendar_event_details, repair_meeting_landing_urls, skip_landing_page_resolution

logger = logging.getLogger(__name__)


BACKFILL_IDLE_SECONDS = 60


async def backfill_calendar_events(stop_event: asyncio.Event) -> None:
    """Repair stored calendar mail without freezing the API.

    The repair holds database locks and does synchronous work. Running it on
    the request loop makes health checks, webhooks, and the calendar wait
    behind it, so the work runs on its own thread and loop. This runs until
    stop_event is set — a one-shot pass used to silently strand a large
    backlog (e.g. right after a mailbox's initial import) if the process
    restarted before it finished; nothing ever resumed it.
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


_BATCH_SIZE = 50


def _load_email_id_batch() -> list[int]:
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
                ),
                EmailMessage.calendar_processed_hash.is_(None),
            )
            .order_by(EmailMessage.received_at.asc(), EmailMessage.id.asc())
            .limit(_BATCH_SIZE)
            .all()
        ]
    finally:
        lookup.close()


async def _backfill_calendar_events(
    stop_event: threading.Event, main_loop: asyncio.AbstractEventLoop,
) -> None:
    pending_mailboxes: set[int] = set()

    def publish_pending() -> None:
        for mailbox_id in list(pending_mailboxes):
            _publish_from_backfill(
                main_loop, "calendar.changed", {"action": "backfilled", "mailbox_id": mailbox_id},
            )
        pending_mailboxes.clear()

    while not stop_event.is_set():
        # A fresh session and a fresh query every batch: fresh query picks up
        # mail that arrived mid-run, and a fresh session keeps its identity
        # map from growing across the whole backlog, which otherwise makes
        # commit()'s autoflush dirty-check slower with every batch.
        email_ids = await asyncio.to_thread(_load_email_id_batch)
        if not email_ids:
            publish_pending()
            # Idle mailboxes never hit this repair path from the main loop
            # above, so give it a turn whenever there's nothing new to do.
            db = SessionLocal()
            try:
                repaired_mailboxes = await repair_meeting_landing_urls(db)
                if repaired_mailboxes:
                    pending_mailboxes.update(repaired_mailboxes)
                    db.commit()
            except Exception:
                db.rollback()
                logger.exception("Calendar landing-page repair failed")
            finally:
                db.close()
            publish_pending()
            for _ in range(BACKFILL_IDLE_SECONDS):
                if stop_event.is_set():
                    return
                await asyncio.sleep(1)
            continue

        db = SessionLocal()
        # Resolving a recruiter landing page is a real HTTP round trip (up to
        # ~8s, up to several candidate URLs per message) and isn't gated by
        # use_ai=False — left on, a handful of Paradox/Teamtailor links in one
        # batch of 50 can stall this loop for minutes with no error and no
        # visible progress. Bulk backfill skips it; live single-message
        # processing (webhooks, manual reclassify) is unaffected.
        skip_token = skip_landing_page_resolution.set(True)
        try:
            for index, email_id in enumerate(email_ids):
                if stop_event.is_set():
                    break
                email = db.get(EmailMessage, email_id)
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
                    # Deterministic parsing (ICS, explicit subject times,
                    # meeting links) covers most mail and is near-instant; a
                    # real model call per message is what made a backlog of
                    # thousands take hours and starve this loop's own
                    # progress behind the lock it holds while waiting on it.
                    if await process_calendar_email(db, email, use_ai=False):
                        pending_mailboxes.add(email.mailbox_id)
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("Calendar backfill failed email_id=%s", email_id)
                if index % 5 == 0:
                    publish_pending()
                    await asyncio.sleep(0)
        finally:
            skip_landing_page_resolution.reset(skip_token)
            db.close()
        publish_pending()
