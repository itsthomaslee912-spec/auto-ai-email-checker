from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth.oauth_google import ensure_google_access_token
from app.auth.oauth_microsoft import ensure_microsoft_access_token
from app.models import MailboxConnection, Provider, WebhookSubscription
from app.realtime.gmail_watch import start_gmail_watch
from app.realtime.outlook_subscriptions import create_outlook_subscription


async def register_mailbox_webhook(
    db: Session,
    mailbox: MailboxConnection,
    *,
    access_token: str | None = None,
) -> WebhookSubscription:
    """Register or refresh the provider webhook for one mailbox."""
    if mailbox.provider == Provider.GOOGLE.value:
        token = access_token or await ensure_google_access_token(db, mailbox)
        return await start_gmail_watch(db, mailbox, token)
    if mailbox.provider == Provider.MICROSOFT.value:
        token = access_token or await ensure_microsoft_access_token(db, mailbox)
        return await create_outlook_subscription(db, mailbox, token)
    raise ValueError(f"Unsupported mailbox provider: {mailbox.provider}")
