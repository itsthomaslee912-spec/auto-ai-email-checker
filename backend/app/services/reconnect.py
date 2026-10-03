from __future__ import annotations

from app.models import MailboxConnection

_RECONNECT_MARKERS = ("reconnect", "invalid_grant")


def needs_reconnect(error: str | None) -> bool:
    """Return whether a sync error can only be fixed by signing in to the account again."""
    text = (error or "").casefold()
    return any(marker in text for marker in _RECONNECT_MARKERS)


def clear_reconnect_errors(mailbox: MailboxConnection) -> None:
    """Forget sign-in failures after the account has been signed in again."""
    if needs_reconnect(mailbox.last_sync_error):
        mailbox.last_sync_error = None
    webhook = mailbox.webhook
    if webhook is not None and needs_reconnect(webhook.last_delivery_error):
        webhook.last_delivery_failure_at = None
        webhook.last_delivery_error = None
