from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_QUOTA_RETRY_DELAYS = (2, 4, 8, 16, 30)


def is_transient_quota_error(message: str | None) -> bool:
    """Return whether Gmail rejected the call because the per-minute quota was used up."""
    text = (message or "").casefold()
    return "quota" in text or "rate limit" in text or "ratelimitexceeded" in text


# DNS and socket failures surface as the underlying OS error text (httpx wraps
# ``socket.gaierror`` as ``ConnectError``). They mean the host briefly lost its
# connection to the provider, not that the account or its credentials are broken.
_NETWORK_ERROR_MARKERS = (
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "name or service not known",
    "failed to establish a new connection",
    "max retries exceeded",
    "connection refused",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "no route to host",
    "server disconnected",
    "timed out",
    # Windows WSA codes: getaddrinfo, TRY_AGAIN, timeout, reset, refused,
    # net unreachable.
    "11001",
    "11002",
    "10060",
    "10054",
    "10061",
    "10051",
)


def is_transient_network_error(message: str | None) -> bool:
    """Return whether a sync failed because the host could not reach the provider."""
    text = (message or "").casefold()
    return any(marker in text for marker in _NETWORK_ERROR_MARKERS)


def is_transient_sync_error(message: str | None) -> bool:
    """Return whether a sync error is self-healing and should not alarm the user.

    Quota throttling and momentary network/DNS loss both clear on their own, so
    the auto-sync loop just retries them rather than flagging the account as
    broken.
    """
    return is_transient_quota_error(message) or is_transient_network_error(message)


def _should_retry_gmail(resp: httpx.Response) -> bool:
    if resp.status_code == 429 or resp.status_code in {500, 502, 503, 504}:
        return True
    return resp.status_code == 403 and is_transient_quota_error(resp.text)


def _decode_body_data(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _mime_type(payload: dict) -> str:
    return (payload.get("mimeType") or "").split(";", 1)[0].strip().lower()


def _looks_like_html(value: str) -> bool:
    return bool(re.search(r"</?[a-z][^>]*>", value or "", re.I))


def _is_calendar_text(value: str) -> bool:
    """Outlook/Teams invites put <https://...> links inside the ICS description.

    That is not HTML. Cutting the part there drops DTSTART/DTEND, which sit
    after the description, and the event is then timed from leftover prose.
    """
    return "BEGIN:VCALENDAR" in (value or "").upper()


def split_mixed_plain_html(value: str) -> tuple[str, str]:
    """If plaintext was concatenated with HTML, return (plain, html)."""
    text = value or ""
    match = re.search(r"<[a-z][\s\S]*>", text, re.I)
    if not match:
        return text.strip(), ""
    idx = match.start()
    if idx == 0:
        return "", text.strip()
    return text[:idx].strip(), text[idx:].strip()


_CALENDAR_MIME_TYPES = {"text/calendar", "application/ics", "application/x-ics", "text/x-vcalendar"}


def _is_calendar_part(payload: dict) -> bool:
    """Microsoft Bookings sends the appointment as application/ics "invite.ics"."""
    filename = (payload.get("filename") or "").lower()
    return _mime_type(payload) in _CALENDAR_MIME_TYPES or filename.endswith(".ics")


def _extract_parts(payload: dict) -> tuple[str, str]:
    """Return (plain_text, html)."""
    mime = _mime_type(payload)
    body = payload.get("body") or {}
    data = body.get("data")
    plain, html = "", ""

    if data:
        decoded = _decode_body_data(data)
        if _is_calendar_part(payload) and _is_calendar_text(decoded):
            plain = decoded
        elif mime == "text/plain":
            if _is_calendar_text(decoded):
                plain = decoded
            else:
                p, h = split_mixed_plain_html(decoded)
                plain, html = p, h
        elif mime == "text/html" or mime.endswith("+html"):
            html = decoded
        elif "html" in mime:
            html = decoded
        elif mime.startswith("text/") and decoded:
            plain = decoded

    for part in payload.get("parts") or []:
        p, h = _extract_parts(part)
        calendar = _is_calendar_text(p)
        if p and _looks_like_html(p) and not h and not calendar:
            extra_plain, extra_html = split_mixed_plain_html(p)
            p, h = extra_plain, extra_html or h
        if p and not plain:
            plain = p
        elif p and p not in plain and (calendar or not _looks_like_html(p)):
            plain = f"{plain}\n{p}".strip()
        if h and not html:
            html = h
        elif h and len(h) > len(html):
            html = h
    return plain.strip(), html.strip()


def _calendar_attachment_parts(payload: dict) -> list[dict]:
    """Return calendar MIME parts whose content must be fetched separately."""
    matches: list[dict] = []
    stack = [payload]
    while stack:
        part = stack.pop()
        body = part.get("body") or {}
        filename = (part.get("filename") or "").lower()
        if _is_calendar_part(part) and body.get("attachmentId") and not body.get("data"):
            matches.append(part)
        stack.extend(part.get("parts") or [])
    return matches


async def _hydrate_calendar_attachments(
    client: httpx.AsyncClient,
    access_token: str,
    message_id: str,
    payload: dict,
) -> None:
    """Inline small ICS attachments so normalization and calendar extraction see them."""
    headers = {"Authorization": f"Bearer {access_token}"}
    for part in _calendar_attachment_parts(payload):
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        try:
            resp = await client.get(
                f"{GMAIL_API}/users/me/messages/{message_id}/attachments/{attachment_id}",
                headers=headers,
            )
            if resp.status_code >= 400:
                logger.warning("Gmail calendar attachment fetch failed for %s: %s", message_id, resp.status_code)
                continue
            data = resp.json().get("data")
            if data:
                body["data"] = data
                part["body"] = body
        except (httpx.HTTPError, ValueError):
            logger.exception("Gmail calendar attachment fetch failed for %s", message_id)


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_received_header_date(value: str) -> datetime | None:
    """Extract the trailing timestamp from an RFC 5322 Received header."""
    if not value:
        return None
    # Date is after the last ';' — e.g. "from ... by mx.google.com ...; Mon, 21 Sep 2026 14:21:13 -0700"
    stamp = value.rsplit(";", 1)[-1].strip()
    if not stamp:
        return None
    try:
        parsed = parsedate_to_datetime(stamp)
    except Exception:
        return None
    return _as_aware_utc(parsed)


def _latest_received_header_time(headers: list[dict]) -> datetime | None:
    """Return the most recent Received hop time (actual mailbox delivery).

    Sender Date / Gmail internalDate can lag or precede delivery for batched
    marketing mail; Received reflects when Gmail accepted the message.
    """
    latest: datetime | None = None
    for h in headers:
        if (h.get("name") or "").lower() != "received":
            continue
        parsed = _parse_received_header_date(h.get("value") or "")
        if parsed is None:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def _gmail_received_at(raw: dict, headers: list[dict]) -> datetime | None:
    received_at = _latest_received_header_time(headers)
    if received_at is not None:
        return received_at
    if raw.get("internalDate"):
        try:
            return datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=timezone.utc)
        except Exception:
            pass
    date_raw = _header(headers, "Date")
    if date_raw:
        try:
            return _as_aware_utc(parsedate_to_datetime(date_raw))
        except Exception:
            return None
    return None


async def gmail_get_profile(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GMAIL_API}/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Gmail profile failed: {resp.text}")
        return resp.json()


class GmailHistoryExpired(Exception):
    pass


async def _gmail_get_with_backoff(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict,
    params: dict,
    what: str,
) -> httpx.Response:
    """Retry a Gmail read when the per-user minute quota, a server error, or a
    dropped connection clears shortly.

    httpx can race a pooled keep-alive connection against the server closing
    it, surfacing as RemoteProtocolError ("Server disconnected without
    sending a response") with no HTTP response to inspect. Left unretried,
    one of these during webhook-triggered notification processing aborts the
    whole fetch and flips the account's webhook to "failed", falling back to
    polling until the next successful sync.
    """
    response: httpx.Response | None = None
    last_attempt = len(_QUOTA_RETRY_DELAYS) - 1
    for attempt, delay in enumerate(_QUOTA_RETRY_DELAYS):
        try:
            response = await client.get(url, headers=headers, params=params)
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError, httpx.PoolTimeout) as exc:
            if attempt == last_attempt:
                raise
            logger.warning(
                "%s hit %s, waiting %ss (attempt %s)",
                what, type(exc).__name__, delay, attempt + 1,
            )
            await asyncio.sleep(delay)
            continue
        if not _should_retry_gmail(response) or attempt == last_attempt:
            return response
        logger.warning(
            "%s hit %s, waiting %ss (attempt %s)",
            what, response.status_code, delay, attempt + 1,
        )
        await asyncio.sleep(delay)
    return response


async def gmail_list_history(access_token: str, start_history_id: str) -> dict:
    """Read a complete history round before its returned cursor can be saved."""
    history: list[dict] = []
    page_token: str | None = None
    latest_history_id = start_history_id
    async with httpx.AsyncClient(timeout=45) as client:
        while True:
            params = {
                "startHistoryId": start_history_id,
                "maxResults": 500,
            }
            if page_token:
                params["pageToken"] = page_token
            resp = await _gmail_get_with_backoff(
                client,
                f"{GMAIL_API}/users/me/history",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
                what="Gmail history",
            )
            if resp.status_code == 404:
                raise GmailHistoryExpired("Gmail history cursor expired")
            if resp.status_code >= 400:
                raise HTTPException(status_code=400, detail=f"Gmail history failed: {resp.text}")
            data = resp.json()
            history.extend(data.get("history") or [])
            latest_history_id = str(data.get("historyId") or latest_history_id)
            page_token = data.get("nextPageToken")
            if not page_token:
                return {"history": history, "historyId": latest_history_id}


async def gmail_get_message(access_token: str, message_id: str) -> dict:
    async with httpx.AsyncClient(timeout=45) as client:
        resp = await _gmail_get_with_backoff(
            client,
            f"{GMAIL_API}/users/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "full"},
            what=f"Gmail get {message_id}",
        )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Gmail message no longer exists")
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Gmail get message failed: {resp.text}")
        raw = resp.json()
        await _hydrate_calendar_attachments(client, access_token, message_id, raw.get("payload") or {})
        return raw


async def gmail_get_messages(
    access_token: str,
    message_ids: list[str],
    *,
    concurrency: int = 4,
    format: str = "full",
) -> list[dict]:
    """Fetch Gmail messages concurrently with retry on quota/5xx."""
    if not message_ids:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"format": format}

    def _is_quota(resp: httpx.Response) -> bool:
        return _should_retry_gmail(resp) and resp.status_code in {403, 429}

    async def fetch_one(client: httpx.AsyncClient, mid: str) -> dict | None:
        async with sem:
            delay = 1.5
            for attempt in range(8):
                try:
                    resp = await client.get(
                        f"{GMAIL_API}/users/me/messages/{mid}",
                        headers=headers,
                        params=params,
                    )
                except httpx.HTTPError:
                    if attempt == 7:
                        logger.warning("Gmail get %s failed after retries", mid)
                        return None
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.5, 45)
                    continue
                if _is_quota(resp) or resp.status_code in {500, 502, 503, 504}:
                    logger.warning(
                        "Gmail get %s hit %s, waiting %.0fs (attempt %s)",
                        mid,
                        resp.status_code,
                        delay,
                        attempt + 1,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.7, 60)
                    continue
                if resp.status_code >= 400:
                    logger.warning("Gmail get %s failed: %s", mid, resp.text[:180])
                    return None
                raw = resp.json()
                await _hydrate_calendar_attachments(client, access_token, mid, raw.get("payload") or {})
                return raw
            return None

    async with httpx.AsyncClient(timeout=45) as client:
        fetched = await asyncio.gather(*[fetch_one(client, mid) for mid in message_ids])
    return [row for row in fetched if row]


# Sent is required: candidate replies that propose interview times only exist there.
GMAIL_COMBINED_QUERY = (
    "in:inbox OR in:spam OR in:trash OR in:sent OR "
    "(-in:inbox -in:spam -in:trash -in:sent -in:drafts)"
)
GMAIL_FOLDER_QUERIES: tuple[tuple[str, str], ...] = (
    ("archive", "-in:inbox -in:spam -in:trash -in:sent -in:drafts"),
    ("inbox", "in:inbox"),
    ("spam", "in:spam"),
    ("sent", "in:sent"),
    ("trash", "in:trash"),
)


async def _gmail_list_ids_for_query(
    client: httpx.AsyncClient,
    access_token: str,
    query: str,
    max_results: int,
) -> tuple[list[str], int]:
    ids: list[str] = []
    page_token: str | None = None
    remaining = max(1, max_results)
    estimate = 0
    while remaining > 0:
        page_size = min(500, remaining)
        params: dict = {"maxResults": page_size, "q": query}
        if page_token:
            params["pageToken"] = page_token
        resp = await client.get(
            f"{GMAIL_API}/users/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Gmail list messages failed: {resp.text}")
        data = resp.json()
        if not estimate:
            estimate = int(data.get("resultSizeEstimate") or 0)
        batch = [m["id"] for m in (data.get("messages") or [])]
        if not batch:
            break
        ids.extend(batch)
        remaining = max_results - len(ids)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return ids[:max_results], max(estimate, len(ids[:max_results]))


async def gmail_list_recent_message_ids(access_token: str, max_results: int = 10000) -> list[str]:
    ids, _estimate = await gmail_list_inbox_message_ids(access_token, max_results=max_results)
    return ids


def _gmail_mailbox_query(query: str, recipient: str | None) -> str:
    """Scope a Gmail search to one connected account.

    Incoming mail is addressed/delivered to the account. Sent mail is from the
    account, so a recipient-only filter would drop every Sent reply (including
    proposed interview times).
    """
    address = (recipient or "").strip()
    if not address:
        return query
    incoming = (
        f"({query}) (to:{address} OR cc:{address} OR bcc:{address} OR deliveredto:{address})"
    )
    if "in:sent" in query.split():
        return f"({incoming}) OR (in:sent from:{address})"
    return incoming


# Older imports still call the previous name.
_gmail_recipient_query = _gmail_mailbox_query


async def gmail_list_inbox_message_ids(
    access_token: str, max_results: int = 10000, recipient: str | None = None,
) -> tuple[list[str], int]:
    """Page through Inbox, Spam, Trash, Sent, and Archive until max_results ids (Gmail max 500 per page)."""
    async with httpx.AsyncClient(timeout=60) as client:
        query = _gmail_mailbox_query(GMAIL_COMBINED_QUERY, recipient)
        return await _gmail_list_ids_for_query(client, access_token, query, max_results)


async def gmail_list_folder_id_map(
    access_token: str, max_results: int = 10000, recipient: str | None = None,
) -> dict[str, str]:
    """Map message ids to inbox/spam/trash/sent/archive. Later folders overwrite (trash wins)."""
    async with httpx.AsyncClient(timeout=60) as client:
        results = await asyncio.gather(
            *(
                _gmail_list_ids_for_query(client, access_token, _gmail_mailbox_query(query, recipient), max_results)
                for _folder, query in GMAIL_FOLDER_QUERIES
            )
        )
    mapping: dict[str, str] = {}
    for (folder, _query), (ids, _estimate) in zip(GMAIL_FOLDER_QUERIES, results):
        for mid in ids:
            mapping[mid] = folder
    return mapping


def normalize_gmail_message(raw: dict) -> dict:
    payload = raw.get("payload") or {}
    headers = payload.get("headers") or []
    subject = _header(headers, "Subject")
    sender = _header(headers, "From")
    # Prefer Received-header delivery time: Date/internalDate can be hours early
    # for delayed/batched mail (e.g. ClinchTalent / Waymo digests).
    received_at = _gmail_received_at(raw, headers)

    body_text, body_html = _extract_parts(payload)
    if body_text and not body_html:
        body_text, body_html = split_mixed_plain_html(body_text)
    if not body_text and body_html:
        # Fallback plain from html tags for classification / preview
        body_text = (
            body_html.replace("<br>", "\n")
            .replace("<br/>", "\n")
            .replace("<br />", "\n")
        )
        out: list[str] = []
        in_tag = False
        for ch in body_text:
            if ch == "<":
                in_tag = True
                continue
            if ch == ">":
                in_tag = False
                continue
            if not in_tag:
                out.append(ch)
        body_text = "".join(out).strip()

    snippet = (raw.get("snippet") or body_text[:280]).strip()
    from app.email.folders import folder_from_gmail_labels

    return {
        "provider_message_id": raw["id"],
        "thread_id": raw.get("threadId"),
        "subject": subject,
        "sender": sender,
        "received_at": received_at,
        "snippet": snippet,
        "body_text": body_text[:20000],
        "body_html": body_html[:200000],
        "folder": folder_from_gmail_labels(raw.get("labelIds")),
        # Gmail marks unread messages with the UNREAD system label.
        "is_read": "UNREAD" not in set(raw.get("labelIds") or []),
    }


async def gmail_mark_read(access_token: str, message_id: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{GMAIL_API}/users/me/messages/{message_id}/modify",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"removeLabelIds": ["UNREAD"]},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Gmail mark read failed: {resp.text}")


async def gmail_mark_read_many(
    access_token: str,
    message_ids: list[str],
    *,
    on_progress: Callable[[int], Awaitable[None]] | None = None,
) -> None:
    if not message_ids:
        return
    async with httpx.AsyncClient(timeout=30) as client:
        for start in range(0, len(message_ids), 1000):
            chunk = message_ids[start : start + 1000]
            resp = await client.post(
                f"{GMAIL_API}/users/me/messages/batchModify",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"ids": chunk, "removeLabelIds": ["UNREAD"]},
            )
            if resp.status_code >= 400:
                raise HTTPException(status_code=400, detail=f"Gmail mark read failed: {resp.text}")
            if on_progress:
                await on_progress(len(chunk))


async def gmail_reply_headers(access_token: str, message_id: str) -> tuple[str | None, str | None]:
    """Return the original message's (Message-ID, References) so a reply threads for every recipient."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GMAIL_API}/users/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params=[("format", "metadata"), ("metadataHeaders", "Message-ID"), ("metadataHeaders", "References")],
        )
    if resp.status_code >= 400:
        return None, None
    headers = {h["name"].lower(): h["value"] for h in resp.json().get("payload", {}).get("headers", [])}
    return headers.get("message-id"), headers.get("references")


async def gmail_send_message(
    access_token: str,
    *,
    to_address: str,
    subject: str,
    body_text: str,
    from_name: str | None = None,
    from_email: str | None = None,
    attachments: list[dict] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    thread_id: str | None = None,
) -> dict:
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders
    from email.utils import formataddr, formatdate

    message = MIMEMultipart() if attachments else MIMEText(body_text, "plain", "utf-8")
    if attachments:
        message.attach(MIMEText(body_text, "plain", "utf-8"))
        for item in attachments:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(item["content"])
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=item["name"])
            message.attach(part)
    message["To"] = to_address
    message["Subject"] = subject
    if from_email:
        message["From"] = formataddr((from_name, from_email)) if from_name else from_email
    message["Date"] = formatdate(localtime=True)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
    body: dict = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GMAIL_API}/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}"},
            json=body,
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Gmail send failed: {resp.text}")
        return resp.json()
