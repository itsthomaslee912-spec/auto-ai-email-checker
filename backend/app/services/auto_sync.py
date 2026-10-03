from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import MailboxConnection
from app.services.mailbox_sync import get_sync_status, start_mailbox_sync
from app.services.ngrok_status import get_ngrok_status
from app.services.reconnect import needs_reconnect
from app.services.webhook_health import (
    webhook_delivery_status,
)
from app.timeutil import as_utc

logger = logging.getLogger(__name__)
_loop_running = False
_last_check_at: datetime | None = None
_last_error: str | None = None
_polling_reasons: dict[int, str | None] = {}


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
    """Return whether this mailbox has individually verified webhook delivery.

    Provider-wide health is deliberately not used here: one working Outlook or
    Gmail account must never make every other account look healthy.
    """
    if not has_registered_webhook(mailbox, now):
        return False
    webhook = mailbox.webhook
    success = as_utc(webhook.last_delivery_success_at) if webhook else None
    failure = as_utc(webhook.last_delivery_failure_at) if webhook else None
    persisted_healthy = bool(success and (not failure or success >= failure))
    return persisted_healthy


def tunnel_problem(ngrok: dict | None) -> str | None:
    """Return why the ngrok tunnel cannot deliver webhooks, if the app relies on ngrok."""
    if not ngrok or "ngrok" not in str(ngrok.get("expected_url") or "").lower():
        return None
    if ngrok.get("state") == "offline":
        return "ngrok is not running, so webhooks cannot reach this app"
    if ngrok.get("state") == "mismatch":
        return "ngrok is serving a different address than the configured webhook URL"
    return None


def polling_reason(
    mailbox: MailboxConnection, ngrok: dict | None = None, now: datetime | None = None,
) -> str | None:
    """Return why this mailbox needs polling, or None when its webhook is delivering mail."""
    now = now or datetime.now(timezone.utc)
    if not has_registered_webhook(mailbox, now):
        return "Webhook is not registered or has expired"
    problem = tunnel_problem(ngrok)
    if problem:
        return problem
    webhook = mailbox.webhook
    if not has_active_webhook(mailbox, now):
        if webhook.last_delivery_failure_at:
            return f"Webhook delivery failed: {webhook.last_delivery_error or 'unknown error'}"
        return "Webhook has not delivered mail yet"
    quiet_seconds = max(60, getattr(get_settings(), "webhook_quiet_seconds", 1800))
    success = as_utc(webhook.last_delivery_success_at)
    if now - success > timedelta(seconds=quiet_seconds):
        return f"No webhook delivery in the last {quiet_seconds // 60} minutes"
    return None


def _provider_delivery_status(provider: str, mailboxes: list[MailboxConnection]) -> dict:
    status = webhook_delivery_status(provider)
    success = datetime.fromisoformat(status["last_success_at"]) if status["last_success_at"] else None
    failure = datetime.fromisoformat(status["last_failure_at"]) if status["last_failure_at"] else None
    error = status["last_error"]
    for mailbox in mailboxes:
        webhook = mailbox.webhook
        if mailbox.provider != provider or webhook is None:
            continue
        stored_success = as_utc(webhook.last_delivery_success_at)
        stored_failure = as_utc(webhook.last_delivery_failure_at)
        if stored_success and (success is None or stored_success > success):
            success = stored_success
        if stored_failure and (failure is None or stored_failure > failure):
            failure = stored_failure
            error = webhook.last_delivery_error
    healthy = bool(success and (not failure or success >= failure))
    return {
        "healthy": healthy,
        "last_success_at": success.isoformat() if success else None,
        "last_failure_at": failure.isoformat() if failure else None,
        "last_error": error if failure and (not success or failure > success) else None,
    }


async def poll_active_mailboxes() -> int:
    """Poll only accounts whose webhook is not delivering mail.

    A working webhook is the sync path. Polling stays off for an account while
    its subscription is current, the ngrok tunnel is up, and it delivered
    recently. It turns on when the webhook is missing, expired, failing, has
    never delivered, has gone quiet, or the tunnel is down.
    """
    ngrok = await asyncio.to_thread(get_ngrok_status)
    mailbox_ids = []
    with SessionLocal() as db:
        for mailbox in db.query(MailboxConnection).filter(MailboxConnection.is_active.is_(True)).all():
            reason = polling_reason(mailbox, ngrok)
            if reason != _polling_reasons.get(mailbox.id):
                if reason:
                    logger.info("Polling mailbox %s: %s", mailbox.id, reason)
                else:
                    logger.info("Mailbox %s is back on webhook delivery", mailbox.id)
                _polling_reasons[mailbox.id] = reason
            if reason and not needs_reconnect(mailbox.last_sync_error):
                mailbox_ids.append(mailbox.id)

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
    ngrok = get_ngrok_status()
    problems = []
    syncing = False
    webhook_accounts = 0
    registered_webhook_accounts = 0
    polling_accounts = 0
    mailbox_statuses = []
    for mailbox in mailboxes:
        if has_registered_webhook(mailbox):
            registered_webhook_accounts += 1
        webhook_verified = has_active_webhook(mailbox)
        if webhook_verified:
            webhook_accounts += 1
        reason = polling_reason(mailbox, ngrok)
        if reason:
            polling_accounts += 1
        job = get_sync_status(mailbox.id)
        if job["state"] == "running":
            syncing = True
        elif job["state"] == "error" or job.get("failed", 0) > 0:
            problems.append({
                "mailbox_id": mailbox.id,
                "email_address": mailbox.email_address,
                "message": job.get("message") or "Sync failed",
            })
        elif mailbox.last_sync_error:
            problems.append({
                "mailbox_id": mailbox.id,
                "email_address": mailbox.email_address,
                "message": mailbox.last_sync_error,
            })
        mailbox_statuses.append({
            "mailbox_id": mailbox.id,
            "email_address": mailbox.email_address,
            "provider": mailbox.provider,
            "state": job["state"],
            "last_sync_started_at": (
                as_utc(mailbox.last_sync_started_at).isoformat()
                if mailbox.last_sync_started_at else None
            ),
            "last_sync_completed_at": (
                as_utc(mailbox.last_sync_completed_at).isoformat()
                if mailbox.last_sync_completed_at else None
            ),
            "last_error": mailbox.last_sync_error,
            "needs_reconnect": needs_reconnect(mailbox.last_sync_error)
            or (job["state"] == "error" and needs_reconnect(job.get("message"))),
            "webhook_registered": has_registered_webhook(mailbox),
            "webhook_verified": webhook_verified,
            "sync_method": "polling" if reason else "webhook",
            "polling_reason": reason,
            "last_webhook_success_at": (
                as_utc(mailbox.webhook.last_delivery_success_at).isoformat()
                if mailbox.webhook and mailbox.webhook.last_delivery_success_at
                else None
            ),
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
        provider: _provider_delivery_status(provider, mailboxes)
        for provider in {mailbox.provider for mailbox in mailboxes}
    }
    return {
        "state": state,
        "interval_seconds": interval,
        "connected_accounts": len(mailboxes),
        "webhook_accounts": webhook_accounts,
        "registered_webhook_accounts": registered_webhook_accounts,
        "polling_accounts": polling_accounts,
        "webhook_delivery": provider_delivery,
        "ngrok": ngrok,
        "last_check_at": _last_check_at.isoformat() if _last_check_at else None,
        "last_error": _last_error,
        "problem_mailboxes": problems,
        "mailboxes": mailbox_statuses,
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
