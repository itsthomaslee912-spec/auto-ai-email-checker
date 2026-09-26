from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth.oauth_google import ensure_google_access_token
from app.auth.oauth_microsoft import ensure_microsoft_access_token
from app.config import get_settings
from app.email.gmail import (
    GmailHistoryExpired,
    gmail_get_message,
    gmail_get_messages,
    gmail_get_profile,
    gmail_list_folder_id_map,
    gmail_list_history,
    gmail_list_inbox_message_ids,
    normalize_gmail_message,
)
from app.email.folders import folder_from_outlook_well_known
from app.email.outlook import (
    OutlookDeltaExpired,
    normalize_outlook_message,
    outlook_attach_folder,
    outlook_get_message,
    outlook_list_delta,
    outlook_list_recent,
)
from app.models import EmailMessage, MailboxConnection, Provider
from app.realtime.outlook_subscriptions import parse_outlook_subscription_ids
from app.realtime.sse import publish
from app.services.ingest import ingest_normalized_message
from app.services.webhook_registration import register_mailbox_webhook
from app.timeutil import as_utc

logger = logging.getLogger(__name__)

FETCH_CHUNK = 80
GMAIL_CONCURRENCY = 16
OUTLOOK_FOLDERS = ("inbox", "junkemail", "deleteditems", "archive")

_jobs: dict[int, dict[str, Any]] = {}
_running: set[int] = set()
_cancel_requested: set[int] = set()
_job_lock = asyncio.Lock()
_cursor_locks: dict[int, asyncio.Lock] = {}


def _cursor_lock(mailbox_id: int) -> asyncio.Lock:
    return _cursor_locks.setdefault(mailbox_id, asyncio.Lock())


def _webhook_is_active(mailbox: MailboxConnection) -> bool:
    webhook = mailbox.webhook
    expires_at = as_utc(webhook.expires_at) if webhook else None
    return bool(webhook and webhook.external_id and expires_at and expires_at > datetime.now(timezone.utc))


def _message_sender_is_excluded(mailbox: MailboxConnection, normalized: dict) -> bool:
    try:
        configured = json.loads(get_settings().mail_sync_excluded_senders or "{}")
    except (TypeError, ValueError):
        logger.warning("MAIL_SYNC_EXCLUDED_SENDERS is not valid JSON")
        return False
    if not isinstance(configured, dict):
        return False
    mailbox_address = mailbox.email_address.strip().casefold()
    excluded = configured.get(mailbox_address) or []
    if isinstance(excluded, str):
        excluded = [excluded]
    excluded_addresses = {str(address).strip().casefold() for address in excluded}
    sender_address = parseaddr(str(normalized.get("sender") or ""))[1].strip().casefold()
    return bool(sender_address and sender_address in excluded_addresses)


def _gmail_message_belongs_to_mailbox(mailbox: MailboxConnection, raw: dict) -> bool:
    """Accept only mail addressed or delivered to this connected Gmail account."""
    headers = (raw.get("payload") or {}).get("headers") or []
    if not headers:
        # Gmail full-message responses contain headers. This fallback keeps
        # partial provider/test payloads processable until a full fetch occurs.
        return True
    recipient_headers = {
        "to", "cc", "bcc", "delivered-to", "x-original-to",
        "x-forwarded-to", "envelope-to",
    }
    values = [
        str(header.get("value") or "")
        for header in headers
        if str(header.get("name") or "").strip().casefold() in recipient_headers
    ]
    recipients = {address.strip().casefold() for _name, address in getaddresses(values) if address}
    return mailbox.email_address.strip().casefold() in recipients


def _should_stop(mailbox_id: int) -> bool:
    return mailbox_id in _cancel_requested


def get_sync_status(mailbox_id: int) -> dict[str, Any]:
    return dict(
        _jobs.get(
            mailbox_id,
            {
                "mailbox_id": mailbox_id,
                "state": "idle",
                "listed": 0,
                "skipped": 0,
                "imported": 0,
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


async def start_mailbox_sync(
    mailbox_id: int, *, max_results: int | None = None,
    register_webhook: bool = True, force_full: bool = False,
) -> dict[str, Any]:
    async with _job_lock:
        current = _jobs.get(mailbox_id)
        if mailbox_id in _running or (current and current.get("state") == "running"):
            return get_sync_status(mailbox_id)
        _cancel_requested.discard(mailbox_id)
        _running.add(mailbox_id)
        job = _set_job(
            mailbox_id,
            state="running",
            listed=0,
            skipped=0,
            imported=0,
            failed=0,
            total=0,
            mode="full" if force_full else "incremental",
            message="Starting inbox sync...",
        )
    asyncio.create_task(_run_sync_job(mailbox_id, max_results, register_webhook, force_full))
    return dict(job)


async def request_stop_sync(mailbox_id: int) -> dict[str, Any]:
    async with _job_lock:
        if mailbox_id not in _running:
            return get_sync_status(mailbox_id)
        _cancel_requested.add(mailbox_id)
        job = _set_job(mailbox_id, message="Stopping sync…")
    await publish("sync.progress", dict(job))
    return dict(job)


async def _run_sync_job(
    mailbox_id: int, max_results: int | None = None,
    register_webhook: bool = True, force_full: bool = False,
) -> None:
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
            await publish("sync.done", get_sync_status(mailbox_id))
            return
        async with _cursor_lock(mailbox_id):
            await bootstrap_mailbox(
                db, mailbox, initial_sync=max_results,
                register_webhook=register_webhook, force_full=force_full,
            )
        imported = _jobs[mailbox_id].get("imported", 0)
        if _should_stop(mailbox_id):
            job = _set_job(
                mailbox_id,
                state="stopped",
                message=f"Sync stopped — imported {imported} new messages",
            )
        else:
            job = _set_job(
                mailbox_id,
                state="done",
                message=f"Synced {imported} new messages",
            )
        await publish("sync.done", dict(job))
    except Exception as exc:
        logger.exception("Mailbox sync failed for %s", mailbox_id)
        job = _set_job(mailbox_id, state="error", message=str(exc)[:300])
        await publish("sync.done", dict(job))
    finally:
        _running.discard(mailbox_id)
        _cancel_requested.discard(mailbox_id)
        db.close()


async def bootstrap_mailbox(
    db: Session,
    mailbox: MailboxConnection,
    *,
    initial_sync: int | None = None,
    register_webhook: bool = True,
    force_full: bool = False,
) -> None:
    limit = get_settings().mail_sync_max if force_full or not mailbox.full_sync_completed else (initial_sync or get_settings().mail_sync_max)
    # Register first. A full initial import can take minutes or hours for a large
    # mailbox, and webhook delivery must not wait for that import to finish.
    if register_webhook and not _webhook_is_active(mailbox):
        try:
            await register_mailbox_webhook(db, mailbox)
        except Exception:
            logger.exception("Webhook registration failed for mailbox %s", mailbox.id)

    if mailbox.provider == Provider.GOOGLE.value:
        if force_full or not mailbox.full_sync_completed or not mailbox.sync_cursor:
            await _full_gmail(db, mailbox, limit)
        else:
            try:
                await _incremental_gmail(db, mailbox)
            except GmailHistoryExpired:
                await _full_gmail(db, mailbox, get_settings().mail_sync_max)
        if _should_stop(mailbox.id):
            return
    elif mailbox.provider == Provider.MICROSOFT.value:
        if force_full or not mailbox.full_sync_completed:
            await _full_outlook(db, mailbox, limit)
        else:
            try:
                await _incremental_outlook(db, mailbox)
            except OutlookDeltaExpired:
                await _full_outlook(db, mailbox, get_settings().mail_sync_max)
        if _should_stop(mailbox.id):
            return


async def _existing_ids(db: Session, mailbox_id: int) -> set[str]:
    rows = db.query(EmailMessage.provider_message_id).filter(EmailMessage.mailbox_id == mailbox_id).all()
    return {row[0] for row in rows if row[0]}


def _stamp_listed_outlook_folders(db: Session, mailbox_id: int, messages: list[dict]) -> None:
    id_to_folder = {
        str(raw["id"]): folder_from_outlook_well_known(str(raw.get("_mail_folder") or "inbox"))
        for raw in messages
        if raw.get("id")
    }
    _apply_folder_stamps(db, mailbox_id, id_to_folder)


def _stamp_listed_gmail_folders(db: Session, mailbox_id: int, id_to_folder: dict[str, str]) -> None:
    _apply_folder_stamps(db, mailbox_id, id_to_folder)


def _apply_folder_stamps(db: Session, mailbox_id: int, id_to_folder: dict[str, str]) -> None:
    if not id_to_folder:
        return
    ids = list(id_to_folder)
    changed = False
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        rows = (
            db.query(EmailMessage)
            .filter(
                EmailMessage.mailbox_id == mailbox_id,
                EmailMessage.provider_message_id.in_(chunk),
            )
            .all()
        )
        for row in rows:
            next_folder = id_to_folder.get(row.provider_message_id)
            if next_folder and row.folder != next_folder:
                row.folder = next_folder
                changed = True
    if changed:
        db.commit()


async def _publish_progress(mailbox_id: int, **fields: Any) -> None:
    job = _set_job(mailbox_id, **fields)
    await publish("sync.progress", dict(job))


async def _full_gmail(db: Session, mailbox: MailboxConnection, limit: int) -> None:
    token = await ensure_google_access_token(db, mailbox)
    profile = await gmail_get_profile(token)
    baseline = str(profile.get("historyId") or "")
    if not baseline:
        raise RuntimeError("Gmail did not return a history cursor")
    await _publish_progress(mailbox.id, mode="full", message="Full Gmail rescan...")
    await _sync_gmail(db, mailbox, limit)
    if _should_stop(mailbox.id):
        return
    failed = int(_jobs.get(mailbox.id, {}).get("failed", 0))
    if failed:
        raise RuntimeError(f"{failed} Gmail messages could not be downloaded; retry Sync to fetch them")
    mailbox.sync_cursor = baseline
    mailbox.full_sync_completed = True
    db.commit()


def _gmail_changed_ids(history: dict) -> list[str]:
    changed: dict[str, None] = {}
    deleted: set[str] = set()
    for record in history.get("history") or []:
        for event_name in ("messagesAdded", "labelsAdded", "labelsRemoved"):
            for event in record.get(event_name) or []:
                message = event.get("message") or {}
                mid = str(message.get("id") or "")
                if mid:
                    changed[mid] = None
        for event in record.get("messagesDeleted") or []:
            mid = str((event.get("message") or {}).get("id") or "")
            if mid:
                deleted.add(mid)
    return [mid for mid in changed if mid not in deleted]


async def _incremental_gmail(db: Session, mailbox: MailboxConnection) -> None:
    token = await ensure_google_access_token(db, mailbox)
    history = await gmail_list_history(token, mailbox.sync_cursor)
    ids = _gmail_changed_ids(history)
    imported = 0
    await _publish_progress(
        mailbox.id, mode="incremental", listed=len(ids), total=len(ids),
        message=f"Checking {len(ids)} changed Gmail messages...",
    )
    for start in range(0, len(ids), FETCH_CHUNK):
        if _should_stop(mailbox.id):
            return
        token = await ensure_google_access_token(db, mailbox)
        chunk = ids[start : start + FETCH_CHUNK]
        raws = await gmail_get_messages(token, chunk, concurrency=GMAIL_CONCURRENCY)
        fetched = {str(raw.get("id")): raw for raw in raws if raw.get("id")}
        missing: set[str] = set()
        for mid in chunk:
            if mid in fetched:
                continue
            try:
                fetched[mid] = await gmail_get_message(token, mid)
            except HTTPException as exc:
                if exc.status_code == 404:
                    missing.add(mid)
                    continue
                raise
        for mid in chunk:
            if mid in missing:
                continue
            raw = fetched[mid]
            labels = {str(value).upper() for value in raw.get("labelIds") or []}
            if labels & {"SENT", "DRAFT"}:
                continue
            if not _gmail_message_belongs_to_mailbox(mailbox, raw):
                continue
            normalized = normalize_gmail_message(raw)
            if _message_sender_is_excluded(mailbox, normalized):
                continue
            email = await ingest_normalized_message(db, mailbox, normalized, download_only=True)
            if email:
                imported += 1
        await _publish_progress(
            mailbox.id, imported=imported, listed=len(ids), total=len(ids),
            message=f"Checked {min(start + len(chunk), len(ids))} changed Gmail messages",
        )
    if _should_stop(mailbox.id):
        return
    mailbox.sync_cursor = str(history.get("historyId") or mailbox.sync_cursor)
    db.commit()
    await _publish_progress(mailbox.id, imported=imported, listed=len(ids), total=len(ids),
                            message=f"Imported {imported} new Gmail messages")


async def _sync_gmail(db: Session, mailbox: MailboxConnection, limit: int) -> None:
    token = await ensure_google_access_token(db, mailbox)
    await _publish_progress(mailbox.id, state="running", message="Listing Inbox, Spam, Trash, and Archive...")
    ids, estimate = await gmail_list_inbox_message_ids(
        token, max_results=limit, recipient=mailbox.email_address
    )
    existing = await _existing_ids(db, mailbox.id)
    if existing:
        folder_map = await gmail_list_folder_id_map(
            token, max_results=limit, recipient=mailbox.email_address
        )
        _stamp_listed_gmail_folders(db, mailbox.id, folder_map)
    new_ids = [mid for mid in ids if mid not in existing]
    imported = 0
    failed = 0
    await _publish_progress(
        mailbox.id,
        listed=len(ids),
        skipped=len(ids) - len(new_ids),
        imported=0,
        failed=0,
        total=max(estimate, len(ids)),
        message=f"Downloading {len(new_ids)} new messages...",
    )
    logger.info(
        "Gmail sync mailbox=%s listed=%s new=%s skipped=%s estimate=%s",
        mailbox.id,
        len(ids),
        len(new_ids),
        len(ids) - len(new_ids),
        estimate,
    )

    for start in range(0, len(new_ids), FETCH_CHUNK):
        if _should_stop(mailbox.id):
            await _publish_progress(
                mailbox.id,
                imported=imported,
                failed=failed,
                skipped=len(ids) - len(new_ids),
                listed=len(ids),
                total=max(estimate, len(ids)),
                message="Stopping sync…",
            )
            return
        token = await ensure_google_access_token(db, mailbox)
        chunk = new_ids[start : start + FETCH_CHUNK]
        raws = await gmail_get_messages(token, chunk, concurrency=GMAIL_CONCURRENCY)
        got = {raw.get("id"): raw for raw in raws if raw.get("id")}
        for mid in chunk:
            raw = got.get(mid)
            if raw is None:
                failed += 1
                continue
            if not _gmail_message_belongs_to_mailbox(mailbox, raw):
                continue
            normalized = normalize_gmail_message(raw)
            if _message_sender_is_excluded(mailbox, normalized):
                continue
            email = await ingest_normalized_message(db, mailbox, normalized, download_only=True)
            if email:
                imported += 1
            if (imported + failed) % 10 == 0 or imported + failed == len(new_ids):
                await _publish_progress(
                    mailbox.id,
                    imported=imported,
                    failed=failed,
                    skipped=len(ids) - len(new_ids),
                    listed=len(ids),
                    total=max(estimate, len(ids)),
                    message=f"Imported {imported} of {len(new_ids)} new messages",
                )
    await _publish_progress(
        mailbox.id,
        imported=imported,
        failed=failed,
        skipped=len(ids) - len(new_ids),
        listed=len(ids),
        total=max(estimate, len(ids)),
        message=f"Imported {imported} of {len(new_ids)} new messages",
    )


async def _sync_outlook(db: Session, mailbox: MailboxConnection, limit: int) -> None:
    token = await ensure_microsoft_access_token(db, mailbox)
    await _publish_progress(mailbox.id, state="running", message="Listing Inbox, Junk, Deleted Items, and Archive...")
    messages = await outlook_list_recent(token, top=limit)
    existing = await _existing_ids(db, mailbox.id)
    _stamp_listed_outlook_folders(db, mailbox.id, messages)
    new_messages = [raw for raw in messages if (raw.get("id") or "") not in existing]
    imported = 0
    await _publish_progress(
        mailbox.id,
        listed=len(messages),
        skipped=len(messages) - len(new_messages),
        imported=0,
        failed=0,
        total=len(messages),
        message=f"Importing {len(new_messages)} new messages...",
    )
    for raw in new_messages:
        if _should_stop(mailbox.id):
            await _publish_progress(
                mailbox.id,
                imported=imported,
                skipped=len(messages) - len(new_messages),
                listed=len(messages),
                total=len(messages),
                message="Stopping sync…",
            )
            return
        email = await ingest_normalized_message(
            db, mailbox, normalize_outlook_message(raw), download_only=True
        )
        if email:
            imported += 1
        if imported % 20 == 0:
            await _publish_progress(
                mailbox.id,
                imported=imported,
                skipped=len(messages) - len(new_messages),
                listed=len(messages),
                total=len(messages),
                message=f"Imported {imported} of {len(new_messages)} new messages",
            )
    await _publish_progress(
        mailbox.id,
        imported=imported,
        skipped=len(messages) - len(new_messages),
        listed=len(messages),
        total=len(messages),
        message=f"Imported {imported} of {len(new_messages)} new messages",
    )


def _outlook_cursors(mailbox: MailboxConnection) -> dict[str, str]:
    if not mailbox.sync_cursor:
        return {}
    try:
        data = json.loads(mailbox.sync_cursor)
    except (TypeError, ValueError) as exc:
        raise OutlookDeltaExpired("Outlook cursor data is invalid") from exc
    if not isinstance(data, dict):
        raise OutlookDeltaExpired("Outlook cursor data is invalid")
    return {folder: str(data[folder]) for folder in OUTLOOK_FOLDERS if data.get(folder)}


async def _sync_outlook_delta(db: Session, mailbox: MailboxConnection) -> None:
    cursors = _outlook_cursors(mailbox)
    imported = int(_jobs.get(mailbox.id, {}).get("imported", 0))
    listed = int(_jobs.get(mailbox.id, {}).get("listed", 0))
    for folder in OUTLOOK_FOLDERS:
        cursor = cursors.get(folder)
        while True:
            if _should_stop(mailbox.id):
                return
            token = await ensure_microsoft_access_token(db, mailbox)
            page = await outlook_list_delta(token, folder, cursor)
            for item in page.get("value") or []:
                if _should_stop(mailbox.id):
                    return
                mid = item.get("id")
                if not mid or item.get("@removed"):
                    continue
                listed += 1
                try:
                    raw = await outlook_get_message(token, str(mid))
                except HTTPException as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                raw["_mail_folder"] = folder
                email = await ingest_normalized_message(db, mailbox, normalize_outlook_message(raw), download_only=True)
                if email:
                    imported += 1
            next_cursor = page.get("@odata.nextLink") or page.get("@odata.deltaLink")
            if not next_cursor:
                raise RuntimeError(f"Outlook delta did not return a cursor for {folder}")
            cursors[folder] = str(next_cursor)
            mailbox.sync_cursor = json.dumps(cursors)
            db.commit()
            await _publish_progress(
                mailbox.id, listed=listed, imported=imported, total=listed,
                message=f"Checked {listed} Outlook changes across folders",
            )
            if page.get("@odata.deltaLink"):
                break
            cursor = str(next_cursor)


async def _full_outlook(db: Session, mailbox: MailboxConnection, limit: int) -> None:
    await _publish_progress(mailbox.id, mode="full", message="Full Outlook rescan...")
    await _sync_outlook(db, mailbox, limit)
    if _should_stop(mailbox.id):
        return
    mailbox.sync_cursor = None
    db.commit()
    await _sync_outlook_delta(db, mailbox)
    if _should_stop(mailbox.id):
        return
    mailbox.full_sync_completed = True
    db.commit()


async def _incremental_outlook(db: Session, mailbox: MailboxConnection) -> None:
    await _publish_progress(mailbox.id, mode="incremental", message="Checking Outlook changes...")
    await _sync_outlook_delta(db, mailbox)


async def process_gmail_notification(db: Session, email_address: str, history_id: str | None) -> int:
    normalized_address = (email_address or "").strip().casefold()
    candidates = (
        db.query(MailboxConnection)
        .filter(
            MailboxConnection.provider == Provider.GOOGLE.value,
            MailboxConnection.is_active.is_(True),
        )
        .all()
    )
    mailbox = next(
        (
            candidate
            for candidate in candidates
            if candidate.email_address.strip().casefold() == normalized_address
        ),
        None,
    )
    if mailbox is None:
        logger.warning("No Google mailbox for notification email=%s", email_address)
        return 0

    async with _cursor_lock(mailbox.id):
        token = await ensure_google_access_token(db, mailbox)
        start_id = mailbox.sync_cursor
        created = 0
        if start_id:
            try:
                history = await gmail_list_history(token, start_id)
            except GmailHistoryExpired:
                await start_mailbox_sync(mailbox.id, register_webhook=False, force_full=True)
                return 0
            for mid in _gmail_changed_ids(history):
                try:
                    raw = await gmail_get_message(token, mid)
                except HTTPException as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                labels = {str(value).upper() for value in raw.get("labelIds") or []}
                if labels & {"SENT", "DRAFT"}:
                    continue
                if not _gmail_message_belongs_to_mailbox(mailbox, raw):
                    continue
                # Webhook imports are live messages, so classify them with the
                # provider and model currently selected in Settings.
                normalized = normalize_gmail_message(raw)
                if _message_sender_is_excluded(mailbox, normalized):
                    continue
                email = await ingest_normalized_message(db, mailbox, normalized)
                if email:
                    created += 1
            if history.get("historyId"):
                mailbox.sync_cursor = str(history["historyId"])
                db.commit()
        elif history_id:
            mailbox.sync_cursor = str(history_id)
            db.commit()
        return created


async def process_outlook_notification(db: Session, subscription_id: str, message_id: str) -> int:
    mailbox = None
    for conn in db.query(MailboxConnection).filter(MailboxConnection.is_active.is_(True)).all():
        if conn.webhook and subscription_id in parse_outlook_subscription_ids(conn.webhook.external_id):
            mailbox = conn
            break
    if mailbox is None:
        mailbox = (
            db.query(MailboxConnection)
            .filter(
                MailboxConnection.provider == Provider.MICROSOFT.value,
                MailboxConnection.is_active.is_(True),
            )
            .first()
        )
    if mailbox is None:
        logger.warning("No Outlook mailbox for subscription %s", subscription_id)
        return 0

    token = await ensure_microsoft_access_token(db, mailbox)
    raw = await outlook_get_message(token, message_id)
    raw = await outlook_attach_folder(token, raw)
    email = await ingest_normalized_message(db, mailbox, normalize_outlook_message(raw))
    return 1 if email else 0
