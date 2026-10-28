from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx
from fastapi import HTTPException

GRAPH = "https://graph.microsoft.com/v1.0"
_TRANSIENT_GRAPH_STATUSES = {429, 500, 502, 503, 504}


def _graph_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        try:
            return max(0.0, min(float(response.headers.get("Retry-After", "")), 10.0))
        except (TypeError, ValueError):
            pass
    return 0.5 * (2 ** attempt)


async def _graph_request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    """Retry transient Graph transport, throttling, and server failures.

    A single TLS/connect timeout used to abort the whole mailbox delta pass.
    Keep retries bounded so a real outage is still reported promptly.
    """
    for attempt in range(3):
        response: httpx.Response | None = None
        try:
            call = getattr(client, method.lower())
            response = await call(url, **kwargs)
        except httpx.TransportError:
            if attempt == 2:
                raise
        else:
            if response.status_code not in _TRANSIENT_GRAPH_STATUSES or attempt == 2:
                return response
        await asyncio.sleep(_graph_retry_delay(response, attempt))
    raise RuntimeError("Microsoft Graph retry loop ended unexpectedly")

# Tags whose own text content must never surface: stylesheet/script bodies,
# and document metadata. The old naive "<...>"-only stripper kept this text
# verbatim, which is why many Outlook emails' body_text started with a wall
# of raw CSS ("body, table { font-family: ... }").
_SKIP_CONTENT_TAGS = {"script", "style", "head", "title"}
# Block-level tags get a newline so adjacent elements don't run together.
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "ul", "ol", "blockquote", "section", "article", "header",
    "footer", "hr",
}


class _HtmlTextExtractor(HTMLParser):
    """Converts HTML to plain text without the usual email-HTML failure modes.

    Calendar invites and similar templated mail often include a visually
    hidden span (style="display:none") carrying structured-data text right
    before the real visible content, with no whitespace between them in the
    markup — e.g. Google Calendar's
    '<span style="display:none">Interview with Picogrid</span>This meeting
    will take place...'. A tag-only stripper keeps that hidden text and
    glues it straight onto what follows ("Interview with PicogridThis
    meeting..."), corrupting whatever later reads the company name out of
    it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._tag_stack: list[str] = []
        self._skip_stack: list[bool] = []
        self._hidden_stack: list[bool] = []

    def _is_skipping(self) -> bool:
        return any(self._skip_stack) or any(self._hidden_stack)

    def _push(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): (value or "") for key, value in attrs}
        style = attrs_dict.get("style", "").lower().replace(" ", "")
        is_hidden = (
            "display:none" in style
            or "visibility:hidden" in style
            or "hidden" in attrs_dict
            or attrs_dict.get("aria-hidden", "").lower() == "true"
        )
        self._tag_stack.append(tag)
        self._skip_stack.append(tag in _SKIP_CONTENT_TAGS)
        self._hidden_stack.append(is_hidden)
        if tag in _BLOCK_TAGS and not self._is_skipping():
            self.chunks.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._push(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._push(tag, attrs)
        self._pop(tag)

    def _pop(self, tag: str) -> None:
        # Email HTML is often malformed; search from the end for the nearest
        # matching open tag instead of assuming a clean stack discipline.
        for index in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[index] == tag:
                del self._tag_stack[index]
                del self._skip_stack[index]
                del self._hidden_stack[index]
                break
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        self._pop(tag)

    def handle_data(self, data: str) -> None:
        if not self._is_skipping():
            self.chunks.append(data)


def _strip_html(html: str) -> str:
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        # HTMLParser is lenient and rarely raises; fall back to the old
        # tag-only strip for whatever markup still manages to break it,
        # rather than losing the message body entirely.
        out: list[str] = []
        in_tag = False
        for ch in html or "":
            if ch == "<":
                in_tag = True
                continue
            if ch == ">":
                in_tag = False
                continue
            if not in_tag:
                out.append(ch)
        return "".join(out)
    text = "".join(parser.chunks)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
            # receivedDateTime lets an already-bootstrapped mailbox establish
            # a newly-added folder cursor without re-downloading that folder's
            # entire history.  Recent messages are still fetched by the sync
            # service, while older rows form the baseline only.
            "$select": "id,receivedDateTime",
            "$top": "50",
        }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await _graph_request_with_retry(
            client,
            "GET",
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if delta_link and resp.status_code in {404, 410}:
            raise OutlookDeltaExpired("Outlook delta cursor expired")
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Outlook delta failed: {resp.text}")
        return resp.json()


async def outlook_get_message(
    access_token: str,
    message_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    async def fetch(active_client: httpx.AsyncClient) -> httpx.Response:
        return await _graph_request_with_retry(
            active_client,
            "GET",
            f"{GRAPH}/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if client is None:
        async with httpx.AsyncClient(timeout=30) as owned_client:
            resp = await fetch(owned_client)
    else:
        resp = await fetch(client)

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Outlook message no longer exists")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"Outlook get message failed: {resp.text}")
    return resp.json()


async def outlook_list_recent(
    access_token: str,
    top: int = 500,
    *,
    get_token: Callable[[], Awaitable[str]] | None = None,
) -> list[dict]:
    """List Inbox, Junk Email, Deleted Items, and Archive, then keep the newest `top` overall.

    `access_token` is fetched once by the caller before this starts, but
    listing four folders with pagination can take long enough for it to
    expire partway through — observed as inbox/junk/deleted succeeding and
    only the last folder (archive) failing with InvalidAuthenticationToken.
    `get_token`, when given, is called before every request so a token about
    to expire gets refreshed instead of reused until it fails.
    """
    # Must match mailbox_sync.OUTLOOK_FOLDERS. Sent Items was missing here
    # for a while — the later unbounded delta pass in the same full-sync
    # call backfills it under normal conditions, but a sync interrupted
    # between the two (a crash or restart) left Sent mail unimported with
    # no bounded-listing fallback.
    folders = ("inbox", "junkemail", "deleteditems", "archive", "sentitems")
    collected: dict[str, dict] = {}
    per_folder = max(1, top)
    select = "id,subject,from,toRecipients,receivedDateTime,bodyPreview,body,conversationId,isRead"
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
                if get_token is not None:
                    access_token = await get_token()
                resp = await _graph_request_with_retry(
                    client,
                    "GET",
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
        resp = await _graph_request_with_retry(
            client,
            "GET",
            f"{GRAPH}/me/mailFolders/{parent}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"$select": "wellKnownName,displayName"},
        )
        if resp.status_code < 400:
            data = resp.json()
            raw["_mail_folder"] = data.get("wellKnownName") or data.get("displayName") or "inbox"
    return raw


def _outlook_event_calendar(raw: dict) -> str:
    """Preserve structured meeting times that Graph omits from the body.

    Event-message start/end fields live on ``eventMessageRequest`` rather than
    the base ``message`` type. Selecting only base fields silently discarded
    them, leaving calendar extraction dependent on prose or AI even though
    Outlook had already supplied the authoritative appointment clock.
    """

    start = raw.get("startDateTime") or {}
    end = raw.get("endDateTime") or {}
    start_value = str(start.get("dateTime") or "").strip()
    end_value = str(end.get("dateTime") or "").strip()
    if not start_value or not end_value:
        return ""

    def ics_datetime(value: str, zone: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
        stamp = parsed.strftime("%Y%m%dT%H%M%S")
        if zone.casefold() in {"utc", "etc/utc", "gmt"}:
            return f":{stamp}Z"
        return f";TZID={zone}:{stamp}" if zone else f":{stamp}"

    start_line = ics_datetime(start_value, str(start.get("timeZone") or ""))
    end_line = ics_datetime(end_value, str(end.get("timeZone") or ""))
    if not start_line or not end_line:
        return ""

    meeting_type = str(raw.get("meetingMessageType") or "").casefold()
    cancelled = meeting_type in {"meetingcancelled", "meetingcanceled", "cancelled", "canceled"}
    method = "CANCEL" if cancelled else "REQUEST"
    subject = str(raw.get("subject") or "").replace("\n", " ").replace("\r", " ")
    location = str((raw.get("location") or {}).get("displayName") or "").replace("\n", " ").replace("\r", " ")
    lines = [
        "BEGIN:VCALENDAR",
        f"METHOD:{method}",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        f"DTSTART{start_line}",
        f"DTEND{end_line}",
    ]
    if subject:
        lines.append(f"SUMMARY:{subject}")
    if location:
        lines.append(f"LOCATION:{location}")
    if cancelled:
        lines.append("STATUS:CANCELLED")
    lines.extend(("END:VEVENT", "END:VCALENDAR"))
    return "\n".join(lines)


def normalize_outlook_message(raw: dict) -> dict:
    sender_obj = ((raw.get("from") or {}).get("emailAddress") or {})
    sender = sender_obj.get("address") or sender_obj.get("name") or ""
    recipients = ", ".join(
        (
            f'{address.get("name")} <{address.get("address")}>'
            if address.get("name") and address.get("address")
            else address.get("address") or address.get("name") or ""
        )
        for item in raw.get("toRecipients") or []
        for address in [(item.get("emailAddress") or {})]
        if address.get("address") or address.get("name")
    )
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

    calendar = _outlook_event_calendar(raw)
    if calendar:
        # Keep the structured block inside the storage limit even for a long
        # HTML invitation. The visible prose remains first for classification;
        # calendar extraction scans the full stored text for VCALENDAR.
        available = max(0, 20000 - len(calendar) - 2)
        body_text = f"{body_text[:available]}\n\n{calendar}"

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
        "recipients": recipients,
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


async def _mark_read_batch(client: httpx.AsyncClient, access_token: str, chunk: list[str]) -> None:
    payload = {
        "requests": [
            {
                "id": str(index),
                "method": "PATCH",
                "url": f"/me/messages/{message_id}",
                "headers": {"Content-Type": "application/json"},
                "body": {"isRead": True},
            }
            for index, message_id in enumerate(chunk)
        ]
    }
    # Graph's documented cap is 4 concurrent requests per mailbox per app; a
    # burst can still trip "MailboxConcurrency limit" (429) even within that,
    # so retry with the server's Retry-After instead of failing the batch.
    for attempt in range(4):
        resp = await client.post(
            f"{GRAPH}/$batch",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code == 429 and attempt < 3:
            delay = float(resp.headers.get("Retry-After") or 2 * (attempt + 1))
            await asyncio.sleep(delay)
            continue
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Outlook batch mark read failed: {resp.text}")
        return


async def outlook_mark_read_many(
    access_token: str,
    message_ids: list[str],
    *,
    on_progress: Callable[[int], Awaitable[None]] | None = None,
) -> None:
    """Mark many messages read via Graph's $batch endpoint (max 20 sub-requests per call).

    Marking them one PATCH at a time — a fresh TCP/TLS connection and a full
    round trip per message — is what made "mark all read" slow for Outlook
    while Gmail's batchModify handled the same action in one request. Batches
    of 20 cut an N-message mark-all-read to ceil(N/20) requests; running up
    to 4 of those batches at once (Graph's per-mailbox concurrency cap) cuts
    the remaining wall-clock time by roughly another 4x.
    """
    if not message_ids:
        return
    chunks = [message_ids[start : start + 20] for start in range(0, len(message_ids), 20)]
    gate = asyncio.Semaphore(4)

    async def run_one(chunk: list[str]) -> None:
        async with gate:
            await _mark_read_batch(client, access_token, chunk)
            if on_progress:
                await on_progress(len(chunk))

    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*(run_one(chunk) for chunk in chunks))


async def outlook_reply_message(
    access_token: str,
    message_id: str,
    *,
    to_address: str,
    body_text: str,
    attachments: list[dict] | None = None,
) -> None:
    """Reply inside the original conversation (Graph sets In-Reply-To/References and quotes the original)."""
    message: dict = {"toRecipients": [{"emailAddress": {"address": to_address}}]}
    if attachments:
        message["attachments"] = [
            {"@odata.type": "#microsoft.graph.fileAttachment", "name": item["name"], "contentType": item["content_type"], "contentBytes": item["content_base64"]}
            for item in attachments
        ]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH}/me/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"message": message, "comment": body_text},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Outlook reply failed: {resp.text}")


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
