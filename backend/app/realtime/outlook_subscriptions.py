from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MailboxConnection, Provider, WebhookSubscription

GRAPH = "https://graph.microsoft.com/v1.0"

OUTLOOK_LIVE_FOLDERS = ("Inbox", "JunkEmail", "DeletedItems", "Archive")


def parse_outlook_subscription_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item) for item in data if item]
        if isinstance(data, str) and data:
            return [data]
    except Exception:
        pass
    return [raw]


def dump_outlook_subscription_ids(ids: list[str]) -> str:
    return json.dumps(ids)


async def _create_one_subscription(
    client: httpx.AsyncClient,
    *,
    access_token: str,
    notification_url: str,
    folder: str,
    expiration: datetime,
    client_state: str,
) -> dict:
    payload = {
        "changeType": "created",
        "notificationUrl": notification_url,
        "resource": f"me/mailFolders('{folder}')/messages",
        "expirationDateTime": expiration.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        "clientState": client_state,
    }
    resp = await client.post(
        f"{GRAPH}/subscriptions",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=payload,
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"Outlook subscription failed ({folder}): {resp.text}")
    return resp.json()


async def create_outlook_subscription(
    db: Session, mailbox: MailboxConnection, access_token: str
) -> WebhookSubscription:
    settings = get_settings()
    notification_url = f"{settings.webhook_base_url.rstrip('/')}/api/webhooks/outlook"
    expiration = datetime.now(timezone.utc) + timedelta(hours=70)

    webhook = mailbox.webhook
    existing_ids = parse_outlook_subscription_ids(webhook.external_id if webhook else None)
    for sub_id in existing_ids:
        try:
            await delete_outlook_subscription(access_token, sub_id)
        except Exception:
            pass

    created: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            for folder in OUTLOOK_LIVE_FOLDERS:
                created.append(
                    await _create_one_subscription(
                        client,
                        access_token=access_token,
                        notification_url=notification_url,
                        folder=folder,
                        expiration=expiration,
                        client_state=settings.microsoft_webhook_client_state,
                    )
                )
        except Exception:
            for item in created:
                sub_id = item.get("id")
                if sub_id:
                    try:
                        await delete_outlook_subscription(access_token, str(sub_id))
                    except Exception:
                        pass
            raise

    if webhook is None:
        webhook = WebhookSubscription(mailbox_id=mailbox.id, provider=Provider.MICROSOFT.value)
        db.add(webhook)

    ids = [str(item.get("id") or "") for item in created if item.get("id")]
    webhook.provider = Provider.MICROSOFT.value
    webhook.external_id = dump_outlook_subscription_ids(ids)
    webhook.resource = ",".join(OUTLOOK_LIVE_FOLDERS)
    expires: list[datetime] = []
    for item in created:
        exp = item.get("expirationDateTime")
        if exp:
            expires.append(datetime.fromisoformat(str(exp).replace("Z", "+00:00")))
    webhook.expires_at = min(expires) if expires else expiration
    db.commit()
    db.refresh(webhook)
    return webhook


async def renew_outlook_subscription(access_token: str, subscription_id: str) -> dict:
    expiration = datetime.now(timezone.utc) + timedelta(hours=70)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"{GRAPH}/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"expirationDateTime": expiration.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Outlook renew failed: {resp.text}")
        return resp.json()


async def renew_outlook_subscriptions(access_token: str, subscription_ids: list[str]) -> datetime | None:
    latest: datetime | None = None
    for sub_id in subscription_ids:
        data = await renew_outlook_subscription(access_token, sub_id)
        exp = data.get("expirationDateTime")
        if exp:
            parsed = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            latest = parsed if latest is None else min(latest, parsed)
    return latest


async def delete_outlook_subscription(access_token: str, subscription_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        await client.delete(
            f"{GRAPH}/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )


async def delete_outlook_subscriptions(access_token: str, subscription_ids: list[str]) -> None:
    for sub_id in subscription_ids:
        try:
            await delete_outlook_subscription(access_token, sub_id)
        except Exception:
            pass
