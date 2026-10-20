from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.classify.openai_classifier import classify_email, infer_interview_subtype
from app.classify.correction_retrieval import load_correction_corpus, retrieve_relevant_corrections
from app.classify.outcome_extract import apply_outcome
from app.email.folders import normalize_folder
from app.models import EmailLabel, EmailMessage, MailboxConnection
from app.realtime.sse import publish
from app.schemas import EmailOut
from app.services.ai_backend import ai_call_semaphore
from app.services.calendar_extract import process_calendar_email
from app.config import get_settings
from app.services.event_bus import enqueue, event_envelope
from app.timeutil import as_utc

logger = logging.getLogger(__name__)


async def ingest_normalized_message(
    db: Session,
    mailbox: MailboxConnection,
    normalized: dict,
    *,
    force_openai: bool = False,
    use_openai: bool = True,
    download_only: bool = False,
    priority: str = "live",
) -> EmailMessage | None:
    existing = (
        db.query(EmailMessage)
        .filter(
            EmailMessage.mailbox_id == mailbox.id,
            EmailMessage.provider_message_id == normalized["provider_message_id"],
        )
        .one_or_none()
    )
    if existing is not None:
        changed = False
        content_changed = False
        folder = normalize_folder(normalized.get("folder"))
        if existing.folder != folder:
            existing.folder = folder
            changed = True
        new_received = as_utc(normalized.get("received_at"))
        if new_received is not None and existing.received_at != new_received:
            existing.received_at = new_received
            changed = True
        # Reading in the app is persisted locally before the provider update
        # finishes. A sync can race that update (and older Outlook fetches did
        # not request isRead at all), so provider refreshes may promote unread
        # to read but must never reset an already-read local message.
        provider_is_read = normalized.get("is_read")
        if provider_is_read is True and not existing.is_read:
            existing.is_read = True
            changed = True
        # A provider can return a richer version of an existing message later
        # (most importantly after fetching a detached text/calendar part).
        # Keep user classification fields intact, but refresh provider-owned
        # message content so calendar extraction can see the complete invite.
        provider_fields = {
            "thread_id": normalized.get("thread_id"),
            "subject": normalized.get("subject") or "",
            "sender": normalized.get("sender") or "",
            "snippet": (normalized.get("snippet") or "")[:1000],
            "body_text": normalized.get("body_text") or "",
            "body_html": normalized.get("body_html") or "",
        }
        for field, value in provider_fields.items():
            if getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True
                content_changed = True
        if changed:
            if content_changed:
                # Make the changed content visible to the calendar processor;
                # process_calendar_email will preserve label corrections.
                db.flush()
                try:
                    # Keep a single malformed legacy item from invalidating the
                    # provider-content refresh or the rest of a bulk repair.
                    with db.begin_nested():
                        await process_calendar_email(
                            db, existing, force=True, use_ai=not download_only, priority=priority,
                        )
                except Exception:
                    logger.exception("Calendar refresh failed during existing-message ingest email_id=%s", existing.id)
            db.commit()
            from app.api.emails import invalidate_email_list_caches

            invalidate_email_list_caches()
        return None

    classification = None
    if not download_only:
        ai_only = getattr(get_settings(), "email_classification_mode", "hybrid").strip().lower() == "ai_only"
        if ai_only:
            correction_examples = []
        else:
            corpus = load_correction_corpus(db)
            correction_examples = retrieve_relevant_corrections(
                corpus,
                subject=normalized.get("subject") or "",
                sender=normalized.get("sender") or "",
                body_text=normalized.get("body_text") or "",
                snippet=normalized.get("snippet") or "",
            )
        try:
            # Live classification uses the active SQLite prompt. Every AI call
            # — live ingest, bulk sync, or background reclassify — shares one
            # priority-ordered gate so a full mailbox import cannot flood a
            # local model or delay a live webhook email behind its backlog.
            async with ai_call_semaphore(priority):
                classification = await classify_email(
                    subject=normalized.get("subject") or "",
                    sender=normalized.get("sender") or "",
                    body_text=normalized.get("body_text") or "",
                    snippet=normalized.get("snippet") or "",
                    folder=normalize_folder(normalized.get("folder")),
                    account_email=mailbox.email_address,
                    correction_examples=correction_examples,
                    force_openai=force_openai,
                    use_openai=use_openai,
                )
        except Exception:
            logger.exception("Classification failed; using heuristic")
            async with ai_call_semaphore(priority):
                classification = await classify_email(
                    subject=normalized.get("subject") or "",
                    sender=normalized.get("sender") or "",
                    body_text=normalized.get("body_text") or "",
                    snippet=normalized.get("snippet") or "",
                    folder=normalize_folder(normalized.get("folder")),
                    account_email=mailbox.email_address,
                    correction_examples=correction_examples,
                    use_openai=False,
                )

    email = EmailMessage(
        mailbox_id=mailbox.id,
        provider_message_id=normalized["provider_message_id"],
        thread_id=normalized.get("thread_id"),
        subject=normalized.get("subject") or "",
        sender=normalized.get("sender") or "",
        received_at=as_utc(normalized.get("received_at")),
        snippet=(normalized.get("snippet") or "")[:1000],
        body_text=normalized.get("body_text") or "",
        body_html=normalized.get("body_html") or "",
        label=classification.label if classification else EmailLabel.OTHER.value,
        category_subtype=classification.category_subtype if classification else None,
        classification_pending=download_only,
        interview_subtype=(
            classification.interview_subtype or (
                None if getattr(get_settings(), "email_classification_mode", "hybrid").strip().lower() == "ai_only" else infer_interview_subtype(
                normalized.get("subject") or "",
                normalized.get("sender") or "",
                normalized.get("body_text") or "",
                normalized.get("snippet") or "",
                )
            ) if classification and classification.label == EmailLabel.INTERVIEW_SCHEDULED.value else None
        ),
        confidence=classification.confidence if classification else None,
        openai_response_id=classification.response_id if classification else None,
        folder=normalize_folder(normalized.get("folder")),
        is_read=bool(normalized.get("is_read", False)),
    )
    db.add(email)
    calendar_changed = False
    try:
        # Flush inside the duplicate-protected transaction. A webhook and a
        # reconciliation sync may race to insert the same provider message.
        db.flush()
        if get_settings().rabbitmq_enabled:
            imported_event = event_envelope(
                "email.imported.v1",
                {
                    "email_id": email.id,
                    "mailbox_id": mailbox.id,
                    "content_version": 1,
                    "classification_pending": bool(download_only),
                },
                user_id=mailbox.user_id,
                aggregate_id=f"email:{email.id}",
                producer="email-ingestion-service",
            )
            enqueue(db, "email.imported.v1", str(mailbox.id), imported_event)
        if not download_only:
            await apply_outcome(db, email, priority=priority)
            db.flush()
            try:
                calendar_changed = await process_calendar_email(db, email, priority=priority)
            except Exception:
                logger.exception("Calendar extraction failed during ingest email_id=%s", email.id)
        db.commit()
        from app.api.emails import invalidate_email_list_caches

        invalidate_email_list_caches()
    except IntegrityError:
        db.rollback()
        # A webhook and the periodic sync can discover the same message while
        # classification is in progress. The other import may have won the race.
        duplicate = (
            db.query(EmailMessage.id)
            .filter(
                EmailMessage.mailbox_id == mailbox.id,
                EmailMessage.provider_message_id == normalized["provider_message_id"],
            )
            .first()
        )
        if duplicate is None:
            raise
        return None
    db.refresh(email)

    await publish("email.classified", EmailOut.model_validate(email).model_dump(mode="json"))
    if calendar_changed:
        await publish("calendar.changed", {"action": "extracted", "mailbox_id": email.mailbox_id})
    return email
