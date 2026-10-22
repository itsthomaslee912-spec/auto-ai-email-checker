from __future__ import annotations

from app.models import MailboxConnection

# Match the exact phrasings the OAuth layer emits for a revoked/expired grant
# (see oauth_google.py / oauth_microsoft.py). A bare "reconnect" is too broad:
# an internal PendingRollbackError ("Can't reconnect until invalid transaction
# is rolled back") also contains the word, and matching it wrongly froze the
# mailbox as "sign-in expired" and made auto-sync skip it forever.
_RECONNECT_MARKERS = (
    "invalid_grant",
    "reconnect this account",
    "reconnect mailbox",
)


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
