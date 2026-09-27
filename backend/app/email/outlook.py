from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

GRAPH = "https://graph.microsoft.com/v1.0"


def _strip_html(html: str) -> str:
    # Lightweight strip without extra deps
    out: list[str] = []
    in_tag = False
    for ch in html:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if not in_tag:
            out.append(ch)
    return "".join(out)


class OutlookDeltaExpired(Exception):
    pass


async def outlook_list_delta(
    access_token: str, folder: str = "inbox", delta_link: str | None = None
) -> dict:
    url = delta_link or f"{GRAPH}/me/mailFolders/{folder}/messages/delta"
    if not url.startswith(f"{GRAPH}/"):
        raise ValueError("Invalid Outlook delta cursor URL")
    params = None
    if not delta_link:
        params = {
            "$select": "id",
            "$top": "50",
        }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if delta_link and resp.status_code in {404, 410}:
            raise OutlookDeltaExpired("Outlook delta cursor expired")
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Outlook delta failed: {resp.text}")
        return resp.json()


async def outlook_get_message(access_token: str, message_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GRAPH}/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "$select": "id,subject,from,receivedDateTime,bodyPreview,body,conversationId,parentFolderId",
            },
        )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Outlook message no longer exists")
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Outlook get message failed: {resp.text}")
        return resp.json()


async def outlook_list_recent(access_token: str, top: int = 500) -> list[dict]:
    """List Inbox, Junk Email, Deleted Items, and Archive, then keep the newest `top` overall."""
    folders = ("inbox", "junkemail", "deleteditems", "archive")
    collected: dict[str, dict] = {}
    per_folder = max(1, top)
    select = "id,subject,from,receivedDateTime,bodyPreview,body,conversationId"
    async with httpx.AsyncClient(timeout=60) as client:
        for folder in folders:
            messages: list[dict] = []
            url = f"{GRAPH}/me/mailFolders/{folder}/messages"
            params: dict | None = {
                "$top": min(50, per_folder),
                "$orderby": "receivedDateTime desc",
                "$select": select,
            }
            while len(messages) < per_folder:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params if url.endswith("/messages") else None,
                )
                if resp.status_code >= 400:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Outlook list messages failed ({folder}): {resp.text}",
                    )
                data = resp.json()
                batch = data.get("value") or []
                if not batch:
                    break
                messages.extend(batch)
                next_link = data.get("@odata.nextLink")
                if not next_link:
                    break
                url = next_link
                params = None
            for raw in messages[:per_folder]:
                mid = raw.get("id")
                if mid:
                    raw["_mail_folder"] = folder
                    collected[mid] = raw

    def received_key(raw: dict) -> str:
        return str(raw.get("receivedDateTime") or "")

    ordered = sorted(collected.values(), key=received_key, reverse=True)
    return ordered[:top]


async def outlook_attach_folder(access_token: str, raw: dict) -> dict:
    if raw.get("_mail_folder"):
        return raw
    parent = raw.get("parentFolderId")
    if not parent:
        return raw
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{GRAPH}/me/mailFolders/{parent}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"$select": "wellKnownName,displayName"},
        )
        if resp.status_code < 400:
            data = resp.json()
            raw["_mail_folder"] = data.get("wellKnownName") or data.get("displayName") or "inbox"
    return raw


def normalize_outlook_message(raw: dict) -> dict:
    sender_obj = ((raw.get("from") or {}).get("emailAddress") or {})
    sender = sender_obj.get("address") or sender_obj.get("name") or ""
    received_raw = raw.get("receivedDateTime")
    received_at = None
    if received_raw:
        try:
            parsed = datetime.fromisoformat(str(received_raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            received_at = parsed.astimezone(timezone.utc)
        except Exception:
            received_at = None

    body = raw.get("body") or {}
    content = body.get("content") or ""
    body_html = ""
    if (body.get("contentType") or "").lower() == "html":
        body_html = content
        body_text = _strip_html(content)
    else:
        body_text = content

    snippet = (raw.get("bodyPreview") or body_text[:280]).strip()
    from app.email.folders import folder_from_outlook_well_known, normalize_folder

    folder = "inbox"
    if raw.get("_mail_folder"):
        folder = folder_from_outlook_well_known(str(raw["_mail_folder"]))
    elif raw.get("folder"):
        folder = normalize_folder(str(raw.get("folder")))
    return {
        "provider_message_id": raw["id"],
        "thread_id": raw.get("conversationId"),
        "subject": raw.get("subject") or "",
        "sender": sender,
        "received_at": received_at,
        "snippet": snippet,
        "body_text": body_text[:20000],
        "body_html": body_html[:200000],
        "folder": folder,
        "is_read": bool(raw.get("isRead", False)),
    }


async def outlook_mark_read(access_token: str, message_id: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.patch(
            f"{GRAPH}/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"isRead": True},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Outlook mark read failed: {resp.text}")


async def outlook_send_message(
    access_token: str,
    *,
    to_address: str,
    subject: str,
    body_text: str,
    attachments: list[dict] | None = None,
) -> None:
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
            "attachments": [
                {"@odata.type": "#microsoft.graph.fileAttachment", "name": item["name"], "contentType": item["content_type"], "contentBytes": item["content_base64"]}
                for item in (attachments or [])
            ],
        },
        "saveToSentItems": True,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH}/me/sendMail",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Outlook send failed: {resp.text}")
