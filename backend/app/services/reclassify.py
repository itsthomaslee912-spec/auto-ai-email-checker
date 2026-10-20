from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from app.classify.openai_classifier import classify_email, infer_interview_subtype
from app.classify.correction_retrieval import load_correction_corpus, retrieve_relevant_corrections
from app.classify.outcome_extract import BACKFILL_LABELS, OUTCOME_LABELS, extract_company_role
from app.config import get_settings
from app.models import CalendarEvent, EmailLabel, EmailMessage, MailboxConnection
from app.realtime.sse import publish
from app.schemas import EmailOut
from app.services.ai_backend import AI_CALL_CONCURRENCY, ai_call_semaphore, get_ai_backend
from app.services.calendar_extract import CALENDAR_LABELS, clean_company_name, employer_instead_of_sender_domain, process_calendar_email

logger = logging.getLogger(__name__)

_jobs: dict[int, dict[str, Any]] = {}
_running: set[int] = set()
_cancel_requested: set[int] = set()
_rerun_requested: dict[int, bool] = {}
_job_lock = asyncio.Lock()
_mailbox_gate: asyncio.Semaphore | None = None


def _mailbox_gate_semaphore() -> asyncio.Semaphore:
    """Only one mailbox reclassifies at a time so Ollama and health stay healthy."""
    global _mailbox_gate
    if _mailbox_gate is None:
        _mailbox_gate = asyncio.Semaphore(1)
    return _mailbox_gate


def _should_stop(mailbox_id: int) -> bool:
    if getattr(get_settings(), "distributed_runtime", False):
        from app.services.job_queue import stop_is_requested

        return stop_is_requested(mailbox_id, "reclassify")
    return mailbox_id in _cancel_requested


def get_reclassify_status(mailbox_id: int) -> dict[str, Any]:
    if getattr(get_settings(), "distributed_runtime", False):
        from app.db import SessionLocal
        from app.services.job_queue import get_status

        with SessionLocal() as db:
            return get_status(db, mailbox_id, "reclassify")
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
    if getattr(get_settings(), "distributed_runtime", False):
        from app.services.job_queue import persist

        persist(mailbox_id, "reclassify", job)
    return job


async def _publish_progress(mailbox_id: int, **fields: Any) -> None:
    job = _set_job(mailbox_id, **fields)
    await publish("reclassify.progress", dict(job))


async def start_mailbox_reclassify(mailbox_id: int, *, pending_only: bool = False) -> dict[str, Any]:
    async with _job_lock:
        current = _jobs.get(mailbox_id)
        if mailbox_id in _running or (current and current.get("state") == "running"):
            # A job is already in flight for this mailbox (e.g. two webhook
            # notifications for the same mailbox landed concurrently). Don't
            # silently drop this request — remember to run again once the
            # in-flight job finishes so newly-pending emails still get
            # classified. If any queued request wants a full reclassify
            # (pending_only=False), the rerun must not be pending-only either.
            existing = _rerun_requested.get(mailbox_id)
            _rerun_requested[mailbox_id] = pending_only if existing is None else (existing and pending_only)
            logger.info(
                "Reclassify already running for mailbox %s; queuing rerun (pending_only=%s)",
                mailbox_id,
                _rerun_requested[mailbox_id],
            )
            return get_reclassify_status(mailbox_id)
        _cancel_requested.discard(mailbox_id)
        _rerun_requested.pop(mailbox_id, None)
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
    if getattr(get_settings(), "distributed_runtime", False) and mailbox_id not in _running:
        from app.db import SessionLocal
        from app.services.job_queue import request_stop

        with SessionLocal() as db:
            job = request_stop(db, mailbox_id, "reclassify")
        await publish("reclassify.progress", dict(job))
        return job
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
    """Resume fast-import emails that still need an AI category.

    Mailboxes run one after another so a local model is not flooded and the
    API stays responsive for health checks while classification finishes.
    """
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
    started: list[dict[str, Any]] = []
    for mailbox_id in ids:
        started.append(await start_mailbox_reclassify(mailbox_id, pending_only=True))
        while mailbox_id in _running:
            await asyncio.sleep(1)
    return started


async def _run_reclassify_job(mailbox_id: int, *, pending_only: bool = False) -> None:
    from app.db import SessionLocal

    async with _mailbox_gate_semaphore():
      try:
        def mailbox_exists() -> bool:
            lookup = SessionLocal()
            try:
                return (
                    lookup.query(MailboxConnection.id)
                    .filter(MailboxConnection.id == mailbox_id, MailboxConnection.is_active.is_(True))
                    .first()
                    is not None
                )
            finally:
                lookup.close()

        if not await asyncio.to_thread(mailbox_exists):
            _set_job(mailbox_id, state="error", message="Mailbox not found")
            await publish("reclassify.done", get_reclassify_status(mailbox_id))
            return

        query_filters = [
            EmailMessage.mailbox_id == mailbox_id,
            EmailMessage.human_corrected.is_not(True),
        ]
        if pending_only:
            query_filters.append(EmailMessage.classification_pending.is_(True))

        def load_rows() -> list[SimpleNamespace]:
            lookup = SessionLocal()
            try:
                loaded = (
                    lookup.query(
                        EmailMessage.id,
                        EmailMessage.subject,
                        EmailMessage.sender,
                        EmailMessage.body_text,
                        EmailMessage.snippet,
                        EmailMessage.label,
                        EmailMessage.outcome_extracted,
                        EmailMessage.folder,
                    )
                    .filter(*query_filters)
                    .order_by(EmailMessage.id.desc())
                    .all()
                )
                return [
                    SimpleNamespace(
                        id=row.id,
                        subject=row.subject,
                        sender=row.sender,
                        body_text=row.body_text,
                        snippet=row.snippet,
                        label=row.label,
                        outcome_extracted=row.outcome_extracted,
                        folder=row.folder,
                    )
                    for row in loaded
                ]
            finally:
                lookup.close()

        rows = await asyncio.to_thread(load_rows)
        def load_context():
            lookup = SessionLocal()
            try:
                mailbox = lookup.get(MailboxConnection, mailbox_id)
                return (mailbox.email_address if mailbox else ""), load_correction_corpus(lookup)
            finally:
                lookup.close()

        account_email, correction_corpus = await asyncio.to_thread(load_context)
        total = len(rows)
        backend = await asyncio.to_thread(get_ai_backend)
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

        updated = 0
        processed = 0
        failed = 0

        async def classify_row(
            row: Any,
        ) -> tuple[int, str, Any, tuple[str, str] | None, bool] | None:
            try:
                if getattr(get_settings(), "email_classification_mode", "hybrid").strip().lower() == "ai_only":
                    correction_examples = []
                else:
                    correction_examples = retrieve_relevant_corrections(
                        correction_corpus,
                        subject=row.subject or "",
                        sender=row.sender or "",
                        body_text=row.body_text or "",
                        snippet=row.snippet or "",
                    )
                # A fresh context manager per call: the shared gate is a
                # priority queue, not a reusable semaphore object. A
                # reclassify pass is always background priority so it never
                # delays a live webhook classification arriving meanwhile.
                # extract_company_role below acquires the same gate itself,
                # so it must not be nested inside this one or the two would
                # deadlock each other over the single slot.
                async with ai_call_semaphore("background"):
                    result = await classify_email(
                        subject=row.subject or "",
                        sender=row.sender or "",
                        body_text=row.body_text or "",
                        snippet=row.snippet or "",
                        folder=row.folder or "inbox",
                        account_email=account_email,
                        correction_examples=correction_examples,
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
                        priority="background",
                    )
                return row.id, row.label, result, extracted, should_extract
            except Exception:
                logger.exception("Reclassify failed for email %s", row.id)
                return None

        def apply_outcome(
            email_id: int,
            previous_label: str,
            result: Any,
            extracted: tuple[str, str] | None,
            should_extract: bool,
        ) -> dict[str, Any]:
            """Persist one classification off the event loop so health checks stay responsive."""
            db = SessionLocal()
            try:
                email = db.query(EmailMessage).filter(EmailMessage.id == email_id).one_or_none()
                if email is None:
                    return {"failed": True}
                if email.human_corrected:
                    return {"skipped": True}
                new_subtype = (
                    result.interview_subtype
                    or (
                        None
                        if getattr(get_settings(), "email_classification_mode", "hybrid").strip().lower() == "ai_only"
                        else infer_interview_subtype(email.subject, email.sender, email.body_text, email.snippet)
                    )
                    if result.label == EmailLabel.INTERVIEW_SCHEDULED.value
                    else None
                )
                label_changed = (
                    email.label != result.label
                    or email.category_subtype != getattr(result, "category_subtype", None)
                    or email.confidence != result.confidence
                    or email.interview_subtype != new_subtype
                )
                was_pending = email.classification_pending
                if label_changed:
                    email.label = result.label
                    email.category_subtype = getattr(result, "category_subtype", None)
                    email.interview_subtype = new_subtype
                    email.confidence = result.confidence
                    email.openai_response_id = result.response_id
                outcome_changed = False
                if should_extract and extracted is not None and email.label in OUTCOME_LABELS:
                    company, role = extracted
                    # A recruiter at gkrouse@google.com hiring for Tapestry is not a
                    # Google interview — without this, a reclassify can leave the
                    # recruiting agency's own domain stored as the employer.
                    email.company = clean_company_name(employer_instead_of_sender_domain(email, company))
                    email.job_role = role
                    email.outcome_extracted = True
                    outcome_changed = True
                elif should_extract and label_changed and email.label in OUTCOME_LABELS:
                    email.outcome_extracted = False
                    outcome_changed = True
                if was_pending:
                    email.classification_pending = False
                needs_calendar = previous_label in CALENDAR_LABELS or email.label in CALENDAR_LABELS or email.folder == "sent"
                if label_changed or outcome_changed or was_pending:
                    db.commit()
                else:
                    db.rollback()
                payload = None
                if label_changed or was_pending:
                    db.refresh(email)
                    payload = EmailOut.model_validate(email).model_dump()
                    payload["previous_label"] = previous_label
                    payload["updated"] = True
                    # A fast-import email may not be present in the browser's
                    # active category until its final label is known. Let the
                    # UI insert this otherwise update-shaped event when the
                    # classification pipeline finishes.
                    payload["classification_completed"] = was_pending
                return {
                    "updated": label_changed,
                    "payload": payload,
                    "needs_calendar": needs_calendar,
                    "email_id": email.id,
                    "label": email.label,
                    "folder": email.folder,
                    "previous_label": previous_label,
                }
            finally:
                db.close()

        async def maybe_update_calendar(applied: dict[str, Any]) -> bool:
            if not applied.get("needs_calendar"):
                return False
            db = SessionLocal()
            try:
                email = db.query(EmailMessage).filter(EmailMessage.id == applied["email_id"]).one_or_none()
                if email is None:
                    return False
                if email.label in CALENDAR_LABELS or email.folder == "sent":
                    changed = await process_calendar_email(db, email, force=True, priority="background")
                    # Calendar extraction mutates events and the email's
                    # processed hash but does not own the transaction. Without
                    # this commit, closing the short-lived worker session rolls
                    # the calendar update back even though the UI is notified.
                    db.commit()
                else:
                    sourced = db.query(CalendarEvent).filter(CalendarEvent.source_email_id == email.id).all()
                    for event in sourced:
                        event.is_visible = False
                    changed = bool(sourced)
                    if changed:
                        db.commit()
                return bool(changed)
            finally:
                db.close()

        # Small batches so progress moves even when the model is slow.
        batch_size = AI_CALL_CONCURRENCY
        for start in range(0, total, batch_size):
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
            chunk = rows[start : start + batch_size]
            outcomes = await asyncio.gather(*(classify_row(row) for row in chunk))
            for outcome in outcomes:
                processed += 1
                if outcome is None:
                    failed += 1
                    continue
                email_id, previous_label, result, extracted, should_extract = outcome
                applied = await asyncio.to_thread(
                    apply_outcome, email_id, previous_label, result, extracted, should_extract
                )
                if applied.get("failed"):
                    failed += 1
                    continue
                if applied.get("skipped"):
                    continue
                if applied.get("updated"):
                    updated += 1
                if applied.get("payload"):
                    await publish("email.classified", applied["payload"])
                # Pending catch-up must stay non-blocking; calendar backfill picks
                # up newly labeled mail afterward. Full reclassify still refreshes
                # calendar immediately.
                if not pending_only and await maybe_update_calendar(applied):
                    await publish("calendar.changed", {"action": "reclassified", "mailbox_id": mailbox_id})
                await asyncio.sleep(0)
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
        rerun_pending_only = _rerun_requested.pop(mailbox_id, None)
        if rerun_pending_only is not None:
            asyncio.create_task(start_mailbox_reclassify(mailbox_id, pending_only=rerun_pending_only))
