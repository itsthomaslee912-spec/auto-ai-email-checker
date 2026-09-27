from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from app.classify.outcome_extract import BACKFILL_LABELS, extract_company_role
from app.models import EmailMessage
from app.services.ai_backend import get_ai_backend

logger = logging.getLogger(__name__)

CONCURRENCY = 4


async def backfill_outcomes(stop_event: asyncio.Event) -> None:
    """Fill company and role for closed, screening, and scheduled interview mail."""
    backend = get_ai_backend()
    if not backend.api_key:
        logger.info("Skipping company/role backfill; selected AI provider is not configured")
        return

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(EmailMessage)
            .filter(
                EmailMessage.label.in_(BACKFILL_LABELS),
                EmailMessage.outcome_extracted.is_(False),
            )
            .order_by(EmailMessage.mailbox_id.asc(), EmailMessage.id.asc())
            .all()
        )
        pending: dict[int, list[tuple[int, str, str, str, str]]] = defaultdict(list)
        for row in rows:
            pending[row.mailbox_id].append(
                (
                    row.id,
                    row.subject or "",
                    row.sender or "",
                    row.body_text or "",
                    row.snippet or "",
                )
            )
    finally:
        db.close()

    total = sum(len(items) for items in pending.values())
    if total == 0:
        return
    logger.info(
        "Backfilling company and role for %s emails across %s accounts",
        total,
        len(pending),
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)
    saved = 0

    async def extract_one(
        item: tuple[int, str, str, str, str],
    ) -> tuple[int, tuple[str, str] | None]:
        email_id, subject, sender, body_text, snippet = item
        async with semaphore:
            if stop_event.is_set():
                return email_id, None
            extracted = await extract_company_role(
                subject=subject,
                sender=sender,
                body_text=body_text,
                snippet=snippet,
            )
            return email_id, extracted

    for mailbox_id, items in pending.items():
        if stop_event.is_set():
            break
        for start in range(0, len(items), CONCURRENCY * 4):
            if stop_event.is_set():
                break
            chunk = items[start : start + CONCURRENCY * 4]
            results = await asyncio.gather(*(extract_one(item) for item in chunk))
            write_db = SessionLocal()
            try:
                for email_id, extracted in results:
                    if extracted is None:
                        continue
                    email = (
                        write_db.query(EmailMessage)
                        .filter(EmailMessage.id == email_id)
                        .one_or_none()
                    )
                    if email is None or email.outcome_extracted:
                        continue
                    if email.label not in BACKFILL_LABELS:
                        continue
                    company, role = extracted
                    email.company = company
                    email.job_role = role
                    email.outcome_extracted = True
                    saved += 1
                write_db.commit()
            except Exception:
                logger.exception("Company/role backfill failed for mailbox %s", mailbox_id)
                write_db.rollback()
            finally:
                write_db.close()

    logger.info("Company/role backfill stored %s of %s emails", saved, total)
