from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import MailboxConnection
from app.services.mailbox_sync import get_sync_status, start_mailbox_sync
from app.services.webhook_health import (
    webhook_delivery_is_healthy,
    webhook_delivery_status,
)
from app.timeutil import as_utc

logger = logging.getLogger(__name__)
_loop_running = False
_last_check_at: datetime | None = None
_last_error: str | None = None


def has_registered_webhook(mailbox: MailboxConnection, now: datetime | None = None) -> bool:
    webhook = mailbox.webhook
    expires_at = as_utc(webhook.expires_at) if webhook else None
    return bool(
        webhook
        and webhook.provider == mailbox.provider
        and webhook.external_id
        and expires_at
        and expires_at > (now or datetime.now(timezone.utc))
    )


def has_active_webhook(mailbox: MailboxConnection, now: datetime | None = None) -> bool:
    return has_registered_webhook(mailbox, now) and webhook_delivery_is_healthy(
        mailbox.provider
    )


async def poll_active_mailboxes() -> int:
    """Poll connected mailboxes only when they have no active webhook."""
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        mailbox_ids = [
            mailbox.id
            for mailbox in db.query(MailboxConnection)
            .filter(MailboxConnection.is_active.is_(True))
            .all()
            if not has_active_webhook(mailbox, now)
        ]

    limit = max(1, get_settings().auto_sync_max_messages)
    errors = 0
    for mailbox_id in mailbox_ids:
        try:
            await start_mailbox_sync(
                mailbox_id, max_results=limit, register_webhook=True
            )
        except Exception:
            errors += 1
            logger.exception("Automatic sync could not start for mailbox %s", mailbox_id)
    return errors


def get_auto_sync_status(db: Session) -> dict:
    interval = max(30, get_settings().auto_sync_interval_seconds)
    mailboxes = db.query(MailboxConnection).filter(MailboxConnection.is_active.is_(True)).all()
    problems = []
    syncing = False
    webhook_accounts = 0
    registered_webhook_accounts = 0
    for mailbox in mailboxes:
        if has_registered_webhook(mailbox):
            registered_webhook_accounts += 1
        if has_active_webhook(mailbox):
            webhook_accounts += 1
            continue
        job = get_sync_status(mailbox.id)
        if job["state"] == "running":
            syncing = True
        elif job["state"] == "error" or job.get("failed", 0) > 0:
            problems.append({
                "mailbox_id": mailbox.id,
                "email_address": mailbox.email_address,
                "message": job.get("message") or "Sync failed",
            })

    recent = _last_check_at is not None and datetime.now(timezone.utc) - _last_check_at < timedelta(seconds=interval * 2)
    if not mailboxes:
        state = "no_account"
    elif not _loop_running or not recent:
        state = "stopped"
    elif _last_error or problems:
        state = "error"
    elif syncing:
        state = "syncing"
    else:
        state = "active"
    provider_delivery = {
        provider: webhook_delivery_status(provider)
        for provider in {mailbox.provider for mailbox in mailboxes}
    }
    return {
        "state": state,
        "interval_seconds": interval,
        "connected_accounts": len(mailboxes),
        "webhook_accounts": webhook_accounts,
        "registered_webhook_accounts": registered_webhook_accounts,
        "webhook_delivery": provider_delivery,
        "last_check_at": _last_check_at.isoformat() if _last_check_at else None,
        "last_error": _last_error,
        "problem_mailboxes": problems,
    }


async def run_auto_sync_check() -> None:
    global _last_check_at, _last_error
    try:
        errors = await poll_active_mailboxes()
        _last_error = f"Could not start sync for {errors} account(s)" if errors else None
    except Exception:
        _last_error = "Automatic sync check failed"
        logger.exception("Automatic sync check failed")
    finally:
        _last_check_at = datetime.now(timezone.utc)


async def auto_sync_loop(stop_event: asyncio.Event) -> None:
    global _loop_running
    interval = max(30, get_settings().auto_sync_interval_seconds)
    _loop_running = True
    try:
        while not stop_event.is_set():
            await run_auto_sync_check()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue
    finally:
        _loop_running = False
