from __future__ import annotations

import logging
import base64
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.auth.oauth_google import ensure_google_access_token
from app.classify.outcome_extract import OUTCOME_LABELS, apply_outcome
from app.classify.openai_classifier import infer_interview_subtype
from app.services.ai_backend import get_ai_backend
from app.auth.oauth_microsoft import ensure_microsoft_access_token
from app.db import SessionLocal, get_db
from app.email.gmail import (
    gmail_get_message,
    gmail_mark_read,
    gmail_mark_read_many,
    gmail_send_message,
    split_mixed_plain_html,
)
from app.email.outlook import (
    outlook_attach_folder,
    outlook_get_message,
    outlook_mark_read,
    outlook_send_message,
)
from app.email.folders import VALID_FOLDERS
from app.models import ClassifyCorrection, EmailLabel, EmailMessage, MailboxConnection, MailFolder, Provider
from app.realtime.sse import publish
from app.schemas import (
    EmailDetailOut,
    EmailLabelUpdateIn,
    EmailOut,
    EmailPageOut,
    MarkAllReadOut,
    SendEmailIn,
    SendEmailOut,
    AiReplyOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/emails", tags=["emails"])

InboxTypeLiteral = Literal["default", "unread_first"]
InterviewSubtypeLiteral = Literal[
    "confirmation", "calendar_invite", "reminder", "reschedule", "time_change", "cancellation"
]


def _from_active_mailbox(query):
    return query.join(
        MailboxConnection, MailboxConnection.id == EmailMessage.mailbox_id
    ).filter(MailboxConnection.is_active.is_(True))


def _search_filter(query: str):
    term = query.strip().lower()
    if not term:
        return None
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    needle = f"%{escaped}%"
    columns = (
        EmailMessage.subject,
        EmailMessage.sender,
        EmailMessage.snippet,
        EmailMessage.company,
        EmailMessage.job_role,
    )
    return or_(
        *(func.lower(func.coalesce(column, "")).like(needle, escape="\\") for column in columns)
    )


def _email_from_active_mailbox(db: Session, email_id: int) -> EmailMessage | None:
    return (
        _from_active_mailbox(db.query(EmailMessage))
        .filter(EmailMessage.id == email_id)
        .one_or_none()
    )


def _normalize_cursor_at(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def _apply_default_cursor(page_q, before_id: int, before_received_at: datetime | None):
    if before_received_at is not None:
        cursor_at = _normalize_cursor_at(before_received_at)
        return page_q.filter(
            or_(
                EmailMessage.received_at < cursor_at,
                and_(EmailMessage.received_at == cursor_at, EmailMessage.id < before_id),
                EmailMessage.received_at.is_(None),
            )
        )
    return page_q.filter(and_(EmailMessage.received_at.is_(None), EmailMessage.id < before_id))


def _apply_unread_first_cursor(
    page_q,
    before_id: int,
    before_received_at: datetime | None,
    before_is_read: bool | None,
):
    cursor_read = bool(before_is_read) if before_is_read is not None else False

    def same_bucket_older(is_read_value: bool):
        if before_received_at is not None:
            cursor_at = _normalize_cursor_at(before_received_at)
            return or_(
                and_(
                    EmailMessage.is_read.is_(is_read_value),
                    EmailMessage.received_at < cursor_at,
                ),
                and_(
                    EmailMessage.is_read.is_(is_read_value),
                    EmailMessage.received_at == cursor_at,
                    EmailMessage.id < before_id,
                ),
                and_(
                    EmailMessage.is_read.is_(is_read_value),
                    EmailMessage.received_at.is_(None),
                    EmailMessage.id < before_id,
                ),
            )
        return and_(
            EmailMessage.is_read.is_(is_read_value),
            EmailMessage.received_at.is_(None),
            EmailMessage.id < before_id,
        )

    if not cursor_read:
        # Still in unread bucket, then any read message.
        return page_q.filter(or_(same_bucket_older(False), EmailMessage.is_read.is_(True)))
    return page_q.filter(same_bucket_older(True))


@router.get("", response_model=EmailPageOut)
def list_emails(
    label: str | None = Query(default=None),
    interview_subtype: InterviewSubtypeLiteral | None = Query(default=None),
    mailbox_id: int | None = Query(default=None),
    folder: str | None = Query(default=None),
    q: str | None = Query(default=None),
    inbox_type: InboxTypeLiteral = Query(default="default"),
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = Query(default=None),
    before_received_at: datetime | None = Query(default=None),
    before_is_read: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> EmailPageOut:
    valid = {item.value for item in EmailLabel}
    filtered = _from_active_mailbox(db.query(EmailMessage))
    if mailbox_id is not None:
        filtered = filtered.filter(EmailMessage.mailbox_id == mailbox_id)
    if label and label != "all" and label in valid:
        filtered = filtered.filter(EmailMessage.label == label)
    if interview_subtype is not None:
        filtered = filtered.filter(
            EmailMessage.label == EmailLabel.INTERVIEW_SCHEDULED.value,
            EmailMessage.interview_subtype == interview_subtype,
        )
    if folder and folder != "all" and folder in VALID_FOLDERS:
        filtered = filtered.filter(EmailMessage.folder == folder)
    if q:
        clause = _search_filter(q)
        if clause is not None:
            filtered = filtered.filter(clause)

    total = filtered.count()
    unread_first = inbox_type == "unread_first"
    if unread_first:
        page_q = filtered.order_by(
            EmailMessage.is_read.asc(),
            EmailMessage.received_at.desc(),
            EmailMessage.id.desc(),
        )
    else:
        page_q = filtered.order_by(EmailMessage.received_at.desc(), EmailMessage.id.desc())

    if before_id is not None:
        if unread_first:
            page_q = _apply_unread_first_cursor(
                page_q, before_id, before_received_at, before_is_read
            )
        else:
            page_q = _apply_default_cursor(page_q, before_id, before_received_at)

    rows = page_q.limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        if row.label not in valid:
            row.label = EmailLabel.OTHER.value
        if row.folder not in VALID_FOLDERS:
            row.folder = MailFolder.INBOX.value

    label_q = _from_active_mailbox(db.query(EmailMessage.label, func.count(EmailMessage.id)))
    if mailbox_id is not None:
        label_q = label_q.filter(EmailMessage.mailbox_id == mailbox_id)
    label_counts = {item: 0 for item in valid}
    for lab, count in label_q.group_by(EmailMessage.label):
        key = lab if lab in valid else EmailLabel.OTHER.value
        label_counts[key] = label_counts.get(key, 0) + int(count)

    folder_q = _from_active_mailbox(db.query(EmailMessage.folder, func.count(EmailMessage.id)))
    if mailbox_id is not None:
        folder_q = folder_q.filter(EmailMessage.mailbox_id == mailbox_id)
    folder_counts = {item: 0 for item in VALID_FOLDERS}
    for fold, count in folder_q.group_by(EmailMessage.folder):
        key = fold if fold in VALID_FOLDERS else MailFolder.INBOX.value
        folder_counts[key] = folder_counts.get(key, 0) + int(count)

    mailbox_count_q = _from_active_mailbox(
        db.query(EmailMessage.mailbox_id, func.count(EmailMessage.id))
    )
    mailbox_counts = {
        str(mid): int(count) for mid, count in mailbox_count_q.group_by(EmailMessage.mailbox_id)
    }
    mailbox_unread_q = _from_active_mailbox(
        db.query(EmailMessage.mailbox_id, func.count(EmailMessage.id))
    ).filter(
        EmailMessage.is_read.is_not(True),
        # Sidebar badges represent new messages that are still in each inbox.
        # Do not count unread copies in Sent, Spam, Trash, or Archive.
        EmailMessage.folder == MailFolder.INBOX.value,
    )
    mailbox_unread_counts = {
        str(mid): int(count) for mid, count in mailbox_unread_q.group_by(EmailMessage.mailbox_id)
    }

    return EmailPageOut(
        items=[EmailOut.model_validate(row) for row in rows],
        next_cursor=rows[-1].id if rows else None,
        next_received_at=rows[-1].received_at if rows else None,
        next_is_read=bool(rows[-1].is_read) if rows else None,
        has_more=has_more,
        total=total,
        label_counts=label_counts,
        mailbox_counts=mailbox_counts,
        mailbox_unread_counts=mailbox_unread_counts,
        folder_counts=folder_counts,
    )


@router.post("/mark-all-read", response_model=MarkAllReadOut)
async def mark_all_read(
    mailbox_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> MarkAllReadOut:
    filtered = _from_active_mailbox(db.query(EmailMessage)).filter(EmailMessage.is_read.is_not(True))
    if mailbox_id is not None:
        filtered = filtered.filter(EmailMessage.mailbox_id == mailbox_id)
    unread = filtered.all()
    if not unread:
        return MarkAllReadOut(marked=0)

    mailbox_ids = {row.mailbox_id for row in unread}
    mailboxes = {
        row.id: row
        for row in db.query(MailboxConnection).filter(MailboxConnection.id.in_(mailbox_ids)).all()
    }
    grouped: dict[int, list[str]] = {}
    for row in unread:
        grouped.setdefault(row.mailbox_id, []).append(row.provider_message_id)

    for mid, message_ids in grouped.items():
        mailbox = mailboxes.get(mid)
        if mailbox is None:
            continue
        try:
            if mailbox.provider == Provider.GOOGLE.value:
                token = await ensure_google_access_token(db, mailbox)
                await gmail_mark_read_many(token, message_ids)
            elif mailbox.provider == Provider.MICROSOFT.value:
                token = await ensure_microsoft_access_token(db, mailbox)
                for message_id in message_ids:
                    await outlook_mark_read(token, message_id)
        except Exception:
            logger.exception(
                "Failed to mark provider messages read mailbox_id=%s count=%s",
                mid,
                len(message_ids),
            )

    for row in unread:
        row.is_read = True
    db.commit()
    return MarkAllReadOut(marked=len(unread))


async def _mark_provider_message_read(mailbox_id: int, provider_message_id: str) -> None:
    with SessionLocal() as db:
        mailbox = db.query(MailboxConnection).filter(
            MailboxConnection.id == mailbox_id,
            MailboxConnection.is_active.is_(True),
        ).one_or_none()
        if mailbox is None:
            return
        try:
            if mailbox.provider == Provider.GOOGLE.value:
                token = await ensure_google_access_token(db, mailbox)
                await gmail_mark_read(token, provider_message_id)
            elif mailbox.provider == Provider.MICROSOFT.value:
                token = await ensure_microsoft_access_token(db, mailbox)
                await outlook_mark_read(token, provider_message_id)
        except Exception:
            logger.exception("Failed to mark provider message read mailbox_id=%s", mailbox_id)


@router.get("/{email_id}", response_model=EmailDetailOut)
async def get_email(email_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> EmailMessage:
    email = _email_from_active_mailbox(db, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")

    html = (email.body_html or "").strip()
    text = (email.body_text or "").strip()
    if text and not html:
        plain, extracted = split_mixed_plain_html(text)
        if extracted:
            email.body_text = plain
            email.body_html = extracted
            html = extracted
            text = plain

    has_body = bool(html or text)
    if not has_body:
        mailbox = (
            db.query(MailboxConnection)
            .filter(MailboxConnection.id == email.mailbox_id, MailboxConnection.is_active.is_(True))
            .one_or_none()
        )
        if mailbox is not None:
            try:
                if mailbox.provider == Provider.GOOGLE.value:
                    token = await ensure_google_access_token(db, mailbox)
                    raw = await gmail_get_message(token, email.provider_message_id)
                    from app.email.gmail import normalize_gmail_message

                    normalized = normalize_gmail_message(raw)
                    email.body_text = normalized.get("body_text") or email.body_text or ""
                    email.body_html = normalized.get("body_html") or email.body_html or ""
                    if normalized.get("snippet"):
                        email.snippet = normalized["snippet"][:1000]
                    if normalized.get("folder"):
                        email.folder = normalized["folder"]
                elif mailbox.provider == Provider.MICROSOFT.value:
                    token = await ensure_microsoft_access_token(db, mailbox)
                    raw = await outlook_get_message(token, email.provider_message_id)
                    raw = await outlook_attach_folder(token, raw)
                    from app.email.outlook import normalize_outlook_message

                    normalized = normalize_outlook_message(raw)
                    email.body_text = normalized.get("body_text") or email.body_text or ""
                    email.body_html = normalized.get("body_html") or email.body_html or ""
                    if normalized.get("snippet"):
                        email.snippet = normalized["snippet"][:1000]
                    if normalized.get("folder"):
                        email.folder = normalized["folder"]
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Could not load email body: {exc}") from exc

    if not email.is_read:
        email.is_read = True
        background_tasks.add_task(_mark_provider_message_read, email.mailbox_id, email.provider_message_id)
    if email.label not in {item.value for item in EmailLabel}:
        email.label = EmailLabel.OTHER.value
    db.commit()
    db.refresh(email)
    return email


TRAINING_BODY_CAP = 4000


@router.patch("/{email_id}/label", response_model=EmailDetailOut)
async def update_email_label(
    email_id: int,
    payload: EmailLabelUpdateIn,
    db: Session = Depends(get_db),
) -> EmailMessage:
    email = _email_from_active_mailbox(db, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")

    previous_label = email.label
    previous_subtype = email.interview_subtype
    new_label = payload.label
    if previous_label != new_label:
        email.label = new_label
        email.interview_subtype = (
            payload.interview_subtype or infer_interview_subtype(email.subject, email.sender, email.body_text, email.snippet)
            if new_label == EmailLabel.INTERVIEW_SCHEDULED.value else None
        )
    elif new_label == EmailLabel.INTERVIEW_SCHEDULED.value and payload.interview_subtype is not None:
        email.interview_subtype = payload.interview_subtype

    if payload.save_training and (previous_label != new_label or previous_subtype != email.interview_subtype):
        body = (email.body_text or email.snippet or "")[:TRAINING_BODY_CAP]
        db.add(ClassifyCorrection(
            email_id=email.id,
            previous_label=previous_label,
            corrected_label=new_label,
            previous_subtype=previous_subtype,
            corrected_subtype=email.interview_subtype,
            subject=email.subject or "",
            sender=email.sender or "",
            snippet=email.snippet or "",
            body_text=body,
        ))

    email.human_corrected = True
    if previous_label != new_label and new_label in OUTCOME_LABELS:
        email.outcome_extracted = False
    db.commit()
    db.refresh(email)

    if previous_label != new_label and new_label in OUTCOME_LABELS:
        if await apply_outcome(db, email, force=True):
            db.commit()
            db.refresh(email)

    payload_out = EmailOut.model_validate(email).model_dump()
    payload_out["previous_label"] = previous_label
    payload_out["updated"] = previous_label != new_label
    await publish("email.classified", payload_out)
    return email


@router.post("/send", response_model=SendEmailOut)
async def send_email(payload: SendEmailIn, db: Session = Depends(get_db)) -> SendEmailOut:
    mailbox = (
        db.query(MailboxConnection)
        .filter(
            MailboxConnection.id == payload.mailbox_id,
            MailboxConnection.is_active.is_(True),
        )
        .one_or_none()
    )
    if mailbox is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")

    to_address = payload.to_address.strip()
    if "@" not in to_address:
        raise HTTPException(status_code=400, detail="Invalid recipient email")
    try:
        attachments = [{
            "name": item.name,
            "content_type": item.content_type,
            "content_base64": item.content_base64,
            "content": base64.b64decode(item.content_base64, validate=True),
        } for item in payload.attachments]
    except Exception as exc:
        raise HTTPException(status_code=400, detail="One attachment is invalid") from exc

    if mailbox.provider == Provider.GOOGLE.value:
        token = await ensure_google_access_token(db, mailbox)
        result = await gmail_send_message(
            token,
            to_address=to_address,
            subject=payload.subject.strip() or "(no subject)",
            body_text=payload.body_text,
            from_name=mailbox.display_name,
            from_email=mailbox.email_address,
            attachments=attachments,
        )
        provider_message_id = result.get("id")

    if mailbox.provider == Provider.MICROSOFT.value:
        token = await ensure_microsoft_access_token(db, mailbox)
        await outlook_send_message(
            token,
            to_address=to_address,
            subject=payload.subject.strip() or "(no subject)",
            body_text=payload.body_text,
            attachments=attachments,
        )
        provider_message_id = None

    elif mailbox.provider not in (Provider.GOOGLE.value, Provider.MICROSOFT.value):
        raise HTTPException(status_code=400, detail="Unsupported provider")

    # Save an immediate local Sent copy so the Sent folder updates without waiting
    # for the next provider sync.
    sent = EmailMessage(
        mailbox_id=mailbox.id, provider_message_id=provider_message_id or f"local-sent-{datetime.now(timezone.utc).timestamp()}",
        subject=payload.subject.strip() or "(no subject)", sender=f"To: {to_address}",
        received_at=datetime.now(timezone.utc), snippet=payload.body_text[:500], body_text=payload.body_text,
        folder=MailFolder.SENT.value, is_read=True, label=EmailLabel.OTHER.value,
    )
    db.add(sent)
    db.commit()
    return SendEmailOut(ok=True, provider_message_id=provider_message_id)


@router.post("/{email_id}/ai-reply", response_model=AiReplyOut)
async def ai_reply(email_id: int, db: Session = Depends(get_db)) -> AiReplyOut:
    email = _from_active_mailbox(db.query(EmailMessage)).filter(EmailMessage.id == email_id).one_or_none()
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    backend = get_ai_backend()
    if not backend.api_key:
        raise HTTPException(status_code=400, detail="Configure an AI model before creating a draft")
    response = await backend.client().chat.completions.create(
        model=backend.model,
        temperature=0.4,
        messages=[
            {"role": "system", "content": "Write a concise, professional email reply. Use only supplied context and do not invent facts. Return only the reply body."},
            {"role": "user", "content": f"From: {email.sender}\nSubject: {email.subject}\n\nMessage history/context:\n{(email.body_text or email.snippet)[:8000]}"},
        ],
    )
    return AiReplyOut(body_text=(response.choices[0].message.content or "").strip())
