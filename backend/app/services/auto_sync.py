from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.email.gmail import is_transient_quota_error
from app.models import MailboxConnection
from app.services.mailbox_sync import get_sync_status, start_mailbox_sync
from app.services.ngrok_status import get_ngrok_status
from app.services.reconnect import needs_reconnect
from app.services.webhook_health import (
    webhook_delivery_status,
)
from app.timeutil import as_utc

logger = logging.getLogger(__name__)
# A per-minute Gmail quota miss is not a dead webhook. Hold polling off briefly
# so the next push can retry without competing for the same quota.
_QUOTA_POLL_HOLDOFF = timedelta(minutes=15)
_loop_running = False
_last_check_at: datetime | None = None
_last_error: str | None = None
_polling_reasons: dict[int, str | None] = {}


def _distributed() -> bool:
    return getattr(get_settings(), "distributed_runtime", False)


def _persist_runtime_status(last_check_at: datetime, last_error: str | None) -> None:
    """Record the latest auto-sync poll so other processes can read it."""
    from app.models import SyncRuntimeStatus

    with SessionLocal() as db:
        row = db.get(SyncRuntimeStatus, 1)
        if row is None:
            row = SyncRuntimeStatus(id=1)
            db.add(row)
        row.last_check_at = last_check_at
        row.last_error = str(last_error)[:512] if last_error else None
        db.commit()


def _load_runtime_status() -> tuple[datetime | None, str | None]:
    from app.models import SyncRuntimeStatus

    with SessionLocal() as db:
        row = db.get(SyncRuntimeStatus, 1)
        if row is None:
            return None, None
        return as_utc(row.last_check_at), row.last_error


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
    if webhook is not None and is_transient_quota_error(webhook.last_delivery_error):
        failure = None
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


def _quota_poll_holdoff(mailbox: MailboxConnection, now: datetime) -> bool:
    """Keep a registered webhook when its latest failure is a fresh Gmail quota limit."""
    webhook = mailbox.webhook
    if webhook is None or webhook.last_delivery_failure_at is None:
        return False
    if not is_transient_quota_error(webhook.last_delivery_error):
        return False
    failure = as_utc(webhook.last_delivery_failure_at)
    success = as_utc(webhook.last_delivery_success_at)
    if failure is None or (success is not None and success >= failure):
        return False
    return now - failure < _QUOTA_POLL_HOLDOFF


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
    if _quota_poll_holdoff(mailbox, now):
        return None
    webhook = mailbox.webhook
    if not has_active_webhook(mailbox, now):
        if webhook.last_delivery_failure_at:
            return f"Webhook delivery failed: {webhook.last_delivery_error or 'unknown error'}"
        return "Webhook has not delivered mail yet"
    # A quiet inbox is not a broken webhook. Gmail and Outlook push only when
    # mail arrives, so a registered, verified subscription stays on webhook.
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
        if is_transient_quota_error(webhook.last_delivery_error):
            stored_failure = None
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
    """Poll accounts that need it; reconcile healthy webhooks on a slower safety net.

    Webhooks remain the immediate-delivery path and are polled every tick.
    A healthy, verified webhook additionally gets a much slower periodic
    incremental sync so a provider outage, backend restart, or exhausted
    retry window cannot leave a message missing forever — but it does not
    need the same 120s cadence as the active polling path, since re-running
    it that often only adds sync/classification load without a live webhook
    needing the help. Healthy Gmail watches are not re-registered on any
    safety pass.
    """
    ngrok = await asyncio.to_thread(get_ngrok_status)
    safety_net_seconds = getattr(get_settings(), "auto_sync_webhook_safety_net_seconds", 900)
    safety_net_age = timedelta(seconds=max(60, safety_net_seconds))
    now = datetime.now(timezone.utc)
    mailboxes_to_sync: list[tuple[int, bool]] = []
    with SessionLocal() as db:
        for mailbox in db.query(MailboxConnection).filter(MailboxConnection.is_active.is_(True)).all():
            reason = polling_reason(mailbox, ngrok)
            if reason != _polling_reasons.get(mailbox.id):
                if reason:
                    logger.info("Polling mailbox %s: %s", mailbox.id, reason)
                else:
                    logger.info("Mailbox %s is back on webhook delivery", mailbox.id)
                _polling_reasons[mailbox.id] = reason
            if needs_reconnect(mailbox.last_sync_error):
                continue
            if reason:
                mailboxes_to_sync.append((mailbox.id, True))
                continue
            last_synced = as_utc(mailbox.last_sync_completed_at)
            if last_synced is None or now - last_synced >= safety_net_age:
                mailboxes_to_sync.append((mailbox.id, False))

    limit = max(1, get_settings().auto_sync_max_messages)
    errors = 0
    for mailbox_id, register_webhook in mailboxes_to_sync:
        try:
            await start_mailbox_sync(
                mailbox_id,
                max_results=limit,
                register_webhook=register_webhook,
            )
        except Exception:
            errors += 1
            logger.exception("Automatic sync could not start for mailbox %s", mailbox_id)
    return errors


def get_auto_sync_status(db: Session) -> dict:
    from app.auth.session import mailbox_scope

    interval = max(30, get_settings().auto_sync_interval_seconds)
    mailboxes = db.query(MailboxConnection).filter(mailbox_scope()).all()
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
        elif mailbox.last_sync_error and not is_transient_quota_error(mailbox.last_sync_error):
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
            "last_error": (
                None if is_transient_quota_error(mailbox.last_sync_error) else mailbox.last_sync_error
            ),
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

    # The auto-sync loop runs in the sync-worker process, so the API must read
    # its heartbeat from the database rather than from in-process globals.
    if _distributed():
        last_check_at, last_error = _load_runtime_status()
        loop_running = last_check_at is not None
    else:
        last_check_at, last_error, loop_running = _last_check_at, _last_error, _loop_running

    recent = last_check_at is not None and datetime.now(timezone.utc) - last_check_at < timedelta(seconds=interval * 2)
    if not mailboxes:
        state = "no_account"
    elif not loop_running or not recent:
        state = "stopped"
    elif last_error or problems:
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
        "last_check_at": last_check_at.isoformat() if last_check_at else None,
        "last_error": last_error,
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
        if _distributed():
            try:
                await asyncio.to_thread(_persist_runtime_status, _last_check_at, _last_error)
            except Exception:
                logger.exception("Could not persist auto-sync runtime status")


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
