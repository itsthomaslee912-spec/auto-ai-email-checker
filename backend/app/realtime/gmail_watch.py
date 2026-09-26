from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MailboxConnection, Provider, WebhookSubscription

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


async def start_gmail_watch(db: Session, mailbox: MailboxConnection, access_token: str) -> WebhookSubscription:
    settings = get_settings()
    if not settings.gmail_pubsub_topic:
        raise HTTPException(
            status_code=500,
            detail="GMAIL_PUBSUB_TOPIC is not configured; cannot start Gmail watch",
        )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GMAIL_API}/users/me/watch",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "topicName": settings.gmail_pubsub_topic,
                "labelIds": ["SENT", "DRAFT"],
                "labelFilterAction": "exclude",
            },
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Gmail watch failed: {resp.text}")
        data = resp.json()

    history_id = str(data.get("historyId") or "")
    expiration_ms = data.get("expiration")
    expires_at = None
    if expiration_ms:
        expires_at = datetime.fromtimestamp(int(expiration_ms) / 1000, tz=timezone.utc)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(days=6)

    if not mailbox.sync_cursor:
        mailbox.sync_cursor = history_id or None
    webhook = mailbox.webhook
    if webhook is None:
        webhook = WebhookSubscription(mailbox_id=mailbox.id, provider=Provider.GOOGLE.value)
        db.add(webhook)

    webhook.provider = Provider.GOOGLE.value
    webhook.external_id = history_id
    webhook.resource = settings.gmail_pubsub_topic
    webhook.expires_at = expires_at
    db.commit()
    db.refresh(webhook)
    return webhook


async def stop_gmail_watch(access_token: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(
            f"{GMAIL_API}/users/me/stop",
            headers={"Authorization": f"Bearer {access_token}"},
        )
