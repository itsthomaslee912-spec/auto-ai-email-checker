from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.classify.openai_classifier import classify_email, infer_interview_subtype
from app.classify.outcome_extract import BACKFILL_LABELS, OUTCOME_LABELS, extract_company_role
from app.models import EmailLabel, EmailMessage, MailboxConnection
from app.realtime.sse import publish
from app.schemas import EmailOut
from app.services.ai_backend import get_ai_backend

logger = logging.getLogger(__name__)

CONCURRENCY = 4

_jobs: dict[int, dict[str, Any]] = {}
_running: set[int] = set()
_cancel_requested: set[int] = set()
_job_lock = asyncio.Lock()


def _should_stop(mailbox_id: int) -> bool:
    return mailbox_id in _cancel_requested


def get_reclassify_status(mailbox_id: int) -> dict[str, Any]:
    return dict(
        _jobs.get(
            mailbox_id,
            {
                "mailbox_id": mailbox_id,
                "state": "idle",
                "updated": 0,
                "processed": 0,
                "failed": 0,
                "total": 0,
                "message": "",
            },
        )
    )


def _set_job(mailbox_id: int, **fields: Any) -> dict[str, Any]:
    job = _jobs.get(mailbox_id) or {"mailbox_id": mailbox_id}
    job.update(fields)
    _jobs[mailbox_id] = job
    return job


async def _publish_progress(mailbox_id: int, **fields: Any) -> None:
    job = _set_job(mailbox_id, **fields)
    await publish("reclassify.progress", dict(job))


async def start_mailbox_reclassify(mailbox_id: int, *, pending_only: bool = False) -> dict[str, Any]:
    async with _job_lock:
        current = _jobs.get(mailbox_id)
        if mailbox_id in _running or (current and current.get("state") == "running"):
            return get_reclassify_status(mailbox_id)
        _cancel_requested.discard(mailbox_id)
        _running.add(mailbox_id)
        job = _set_job(
            mailbox_id,
            state="running",
            updated=0,
            processed=0,
            failed=0,
            total=0,
            message="Starting queued reclassification..." if pending_only else "Starting reclassify...",
        )
    asyncio.create_task(_run_reclassify_job(mailbox_id, pending_only=pending_only))
    return dict(job)


async def request_stop_reclassify(mailbox_id: int) -> dict[str, Any]:
    async with _job_lock:
        if mailbox_id not in _running:
            return get_reclassify_status(mailbox_id)
        _cancel_requested.add(mailbox_id)
        job = _set_job(mailbox_id, message="Stopping reclassify…")
    await publish("reclassify.progress", dict(job))
    return dict(job)


async def start_all_active_reclassify() -> list[dict[str, Any]]:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        ids = [
            row[0]
            for row in db.query(MailboxConnection.id)
            .filter(MailboxConnection.is_active.is_(True))
            .all()
        ]
    finally:
        db.close()
    started: list[dict[str, Any]] = []
    for mailbox_id in ids:
        started.append(await start_mailbox_reclassify(mailbox_id))
    return started


async def start_all_pending_reclassify() -> list[dict[str, Any]]:
    """Resume fast-import emails that still need an AI category."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        ids = [
            row[0]
            for row in db.query(MailboxConnection.id)
            .join(EmailMessage, EmailMessage.mailbox_id == MailboxConnection.id)
            .filter(
                MailboxConnection.is_active.is_(True),
                EmailMessage.classification_pending.is_(True),
                EmailMessage.human_corrected.is_not(True),
            )
            .distinct()
            .all()
        ]
    finally:
        db.close()
    return [await start_mailbox_reclassify(mailbox_id, pending_only=True) for mailbox_id in ids]


async def _run_reclassify_job(mailbox_id: int, *, pending_only: bool = False) -> None:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        mailbox = (
            db.query(MailboxConnection)
            .filter(MailboxConnection.id == mailbox_id, MailboxConnection.is_active.is_(True))
            .one_or_none()
        )
        if mailbox is None:
            _set_job(mailbox_id, state="error", message="Mailbox not found")
            await publish("reclassify.done", get_reclassify_status(mailbox_id))
            return

        query = (
            db.query(
                EmailMessage.id,
                EmailMessage.subject,
                EmailMessage.sender,
                EmailMessage.body_text,
                EmailMessage.snippet,
                EmailMessage.label,
                EmailMessage.outcome_extracted,
            )
            .filter(
                EmailMessage.mailbox_id == mailbox_id,
                EmailMessage.human_corrected.is_not(True),
            )
        )
        if pending_only:
            query = query.filter(EmailMessage.classification_pending.is_(True))
        rows = query.order_by(EmailMessage.id.desc()).all()
        total = len(rows)
        backend = get_ai_backend()
        await _publish_progress(
            mailbox_id,
            state="running",
            total=total,
            updated=0,
            processed=0,
            failed=0,
            message=(
                f"Classifying {total} queued emails with {backend.provider.title()}..."
                if pending_only else f"Reclassifying {total} emails with {backend.provider.title()}..."
            ),
        )

        semaphore = asyncio.Semaphore(CONCURRENCY)
        updated = 0
        processed = 0
        failed = 0

        async def classify_row(
            row: Any,
        ) -> tuple[int, str, Any, tuple[str, str] | None, bool] | None:
            async with semaphore:
                try:
                    result = await classify_email(
                        subject=row.subject or "",
                        sender=row.sender or "",
                        body_text=row.body_text or "",
                        snippet=row.snippet or "",
                        force_openai=True,
                    )
                    should_extract = result.label in OUTCOME_LABELS and (
                        result.label != row.label
                        or (result.label in BACKFILL_LABELS and not row.outcome_extracted)
                    )
                    extracted: tuple[str, str] | None = None
                    if should_extract:
                        extracted = await extract_company_role(
                            subject=row.subject or "",
                            sender=row.sender or "",
                            body_text=row.body_text or "",
                            snippet=row.snippet or "",
                        )
                    return row.id, row.label, result, extracted, should_extract
                except Exception:
                    logger.exception("Reclassify failed for email %s", row.id)
                    return None

        for start in range(0, total, CONCURRENCY * 4):
            if _should_stop(mailbox_id):
                job = _set_job(
                    mailbox_id,
                    state="stopped",
                    updated=updated,
                    processed=processed,
                    failed=failed,
                    total=total,
                    message=f"Reclassify stopped — updated {updated} of {total}",
                )
                await publish("reclassify.done", dict(job))
                return
            chunk = rows[start : start + CONCURRENCY * 4]
            outcomes = await asyncio.gather(*(classify_row(row) for row in chunk))
            for outcome in outcomes:
                processed += 1
                if outcome is None:
                    failed += 1
                    continue
                email_id, previous_label, result, extracted, should_extract = outcome
                email = db.query(EmailMessage).filter(EmailMessage.id == email_id).one_or_none()
                if email is None:
                    failed += 1
                    continue
                if email.human_corrected:
                    continue
                new_subtype = (
                    result.interview_subtype or infer_interview_subtype(email.subject, email.sender, email.body_text, email.snippet)
                    if result.label == EmailLabel.INTERVIEW_SCHEDULED.value else None
                )
                label_changed = email.label != result.label or email.confidence != result.confidence or email.interview_subtype != new_subtype
                was_pending = email.classification_pending
                if label_changed:
                    email.label = result.label
                    email.interview_subtype = new_subtype
                    email.confidence = result.confidence
                    email.openai_response_id = result.response_id
                outcome_changed = False
                if should_extract and extracted is not None and email.label in OUTCOME_LABELS:
                    email.company, email.job_role = extracted
                    email.outcome_extracted = True
                    outcome_changed = True
                elif should_extract and label_changed and email.label in OUTCOME_LABELS:
                    email.outcome_extracted = False
                    outcome_changed = True
                if was_pending:
                    email.classification_pending = False
                if label_changed or outcome_changed or was_pending:
                    db.commit()
                    if label_changed:
                        db.refresh(email)
                        updated += 1
                        payload = EmailOut.model_validate(email).model_dump()
                        payload["previous_label"] = previous_label
                        payload["updated"] = True
                        await publish("email.classified", payload)
                else:
                    db.rollback()
            await _publish_progress(
                mailbox_id,
                updated=updated,
                processed=processed,
                failed=failed,
                total=total,
                message=f"Reclassified {min(start + len(chunk), total)} of {total} — {updated} changed",
            )

        job = _set_job(
            mailbox_id,
            state="done",
            updated=updated,
            processed=processed,
            failed=failed,
            total=total,
            message=f"Reclassify complete — updated {updated} of {total}",
        )
        await publish("reclassify.done", dict(job))
    except Exception as exc:
        logger.exception("Mailbox reclassify failed for %s", mailbox_id)
        job = _set_job(mailbox_id, state="error", message=str(exc)[:300])
        await publish("reclassify.done", dict(job))
    finally:
        _running.discard(mailbox_id)
        _cancel_requested.discard(mailbox_id)
        db.close()
