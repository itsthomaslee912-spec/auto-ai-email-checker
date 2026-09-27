from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.auth.oauth_google import ensure_google_access_token
from app.auth.oauth_microsoft import ensure_microsoft_access_token
from app.db import SessionLocal
from app.models import MailboxConnection, Provider, WebhookSubscription
from app.realtime.gmail_watch import start_gmail_watch
from app.realtime.outlook_subscriptions import (
    OUTLOOK_LIVE_FOLDERS,
    create_outlook_subscription,
    parse_outlook_subscription_ids,
    renew_outlook_subscriptions,
)

logger = logging.getLogger(__name__)


async def renew_expiring_subscriptions() -> None:
    db: Session = SessionLocal()
    try:
        soon = datetime.now(timezone.utc) + timedelta(hours=12)
        webhooks = (
            db.query(WebhookSubscription)
            .filter(WebhookSubscription.expires_at.isnot(None))
            .filter(WebhookSubscription.expires_at < soon)
            .all()
        )
        for webhook in webhooks:
            mailbox = db.query(MailboxConnection).filter(MailboxConnection.id == webhook.mailbox_id).one_or_none()
            if mailbox is None or not mailbox.is_active:
                continue
            try:
                if mailbox.provider == Provider.GOOGLE.value:
                    token = await ensure_google_access_token(db, mailbox)
                    await start_gmail_watch(db, mailbox, token)
                elif mailbox.provider == Provider.MICROSOFT.value and webhook.external_id:
                    token = await ensure_microsoft_access_token(db, mailbox)
                    ids = parse_outlook_subscription_ids(webhook.external_id)
                    if len(ids) < len(OUTLOOK_LIVE_FOLDERS):
                        await create_outlook_subscription(db, mailbox, token)
                    else:
                        expires_at = await renew_outlook_subscriptions(token, ids)
                        if expires_at:
                            webhook.expires_at = expires_at
                            db.commit()
            except Exception:
                logger.exception("Failed renewing webhook for mailbox %s", mailbox.id)
    finally:
        db.close()


async def renewal_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await renew_expiring_subscriptions()
        except Exception:
            logger.exception("Renewal loop error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3600)
        except TimeoutError:
            continue
