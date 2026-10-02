from __future__ import annotations

import asyncio
import logging

from app.db import SessionLocal
from app.models import EmailMessage
from app.realtime.sse import publish
from app.services.calendar_extract import CALENDAR_LABELS, email_calendar_hash, process_calendar_email

logger = logging.getLogger(__name__)


async def backfill_calendar_events(stop_event: asyncio.Event) -> None:
    """Extract calendar records for relevant stored mail without blocking startup."""
    db = SessionLocal()
    changed_mailboxes: set[int] = set()
    try:
        rows = (
            db.query(EmailMessage)
            .filter(EmailMessage.label.in_(CALENDAR_LABELS))
            .order_by(EmailMessage.received_at.asc(), EmailMessage.id.asc())
            .all()
        )
        for index, email in enumerate(rows):
            if stop_event.is_set():
                break
            if email.calendar_processed_hash == email_calendar_hash(email):
                continue
            try:
                if await process_calendar_email(db, email):
                    changed_mailboxes.add(email.mailbox_id)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Calendar backfill failed email_id=%s", email.id)
            if index % 20 == 0:
                await asyncio.sleep(0)
    finally:
        db.close()
    for mailbox_id in changed_mailboxes:
        await publish("calendar.changed", {"action": "backfilled", "mailbox_id": mailbox_id})
