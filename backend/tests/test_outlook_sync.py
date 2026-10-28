from __future__ import annotations

import asyncio

import httpx

from app.email.outlook import outlook_get_message, outlook_list_recent


def _folder_response(folder: str) -> dict:
    return {
        "value": [
            {
                "id": f"{folder}-1",
                "subject": f"Message in {folder}",
                "from": {"emailAddress": {"address": "sender@example.com"}},
                "receivedDateTime": "2026-10-13T12:00:00Z",
                "bodyPreview": "",
                "body": {"content": "", "contentType": "text"},
                "conversationId": "conv-1",
            }
        ]
    }


def _patched_async_client(transport: httpx.MockTransport):
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    return patched


def test_outlook_list_recent_calls_get_token_before_every_request():
    """A token fetched once before listing four folders with pagination can
    expire partway through — observed live as inbox/junk/deleted succeeding
    and only the last folder (archive) failing with
    InvalidAuthenticationToken. get_token must be called before each
    request, not just once up front, so a near-expiry token gets refreshed
    in time instead of reused until it fails."""
    call_count = {"n": 0}

    async def get_token() -> str:
        call_count["n"] += 1
        return f"token-{call_count['n']}"

    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["authorization"])
        folder = request.url.path.split("/")[-2]
        return httpx.Response(200, json=_folder_response(folder))

    import app.email.outlook as outlook_module

    original = outlook_module.httpx.AsyncClient
    outlook_module.httpx.AsyncClient = _patched_async_client(httpx.MockTransport(handler))
    try:
        asyncio.run(outlook_list_recent("stale-token", top=5, get_token=get_token))
    finally:
        outlook_module.httpx.AsyncClient = original

    # One call per folder (inbox, junkemail, deleteditems, archive, sentitems);
    # every request used a token fetched fresh via get_token, never the stale
    # one passed in up front.
    assert call_count["n"] == 5
    assert seen_tokens == [f"Bearer token-{i}" for i in range(1, 6)]
    assert "Bearer stale-token" not in seen_tokens


def test_outlook_list_recent_without_get_token_reuses_static_token():
    """Backward compatibility: omitting get_token keeps the old behavior of
    reusing the single access_token passed in."""
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["authorization"])
        folder = request.url.path.split("/")[-2]
        return httpx.Response(200, json=_folder_response(folder))

    import app.email.outlook as outlook_module

    original = outlook_module.httpx.AsyncClient
    outlook_module.httpx.AsyncClient = _patched_async_client(httpx.MockTransport(handler))
    try:
        asyncio.run(outlook_list_recent("fixed-token", top=5))
    finally:
        outlook_module.httpx.AsyncClient = original

    assert seen_tokens == ["Bearer fixed-token"] * 5


def test_outlook_list_queries_request_read_state_and_get_fetches_derived_fields():
    selects: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        selects.append(request.url.params.get("$select", ""))
        if "/mailFolders/" in request.url.path:
            folder = request.url.path.split("/")[-2]
            return httpx.Response(200, json=_folder_response(folder))
        return httpx.Response(200, json={"id": "message-1", "isRead": True})

    import app.email.outlook as outlook_module

    original = outlook_module.httpx.AsyncClient
    outlook_module.httpx.AsyncClient = _patched_async_client(httpx.MockTransport(handler))
    try:
        asyncio.run(outlook_get_message("token", "message-1"))
        asyncio.run(outlook_list_recent("token", top=1))
    finally:
        outlook_module.httpx.AsyncClient = original

    assert len(selects) == 6
    # A single-message fetch intentionally omits $select so Graph returns
    # eventMessageRequest fields such as startDateTime and endDateTime.
    assert selects[0] == ""
    assert all("isRead" in select.split(",") for select in selects[1:])


def test_outlook_get_message_retries_transient_connection_failure(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("temporary TLS timeout", request=request)
        return httpx.Response(200, json={"id": "message-1", "isRead": True})

    async def no_delay(_seconds: float) -> None:
        return None

    import app.email.outlook as outlook_module

    monkeypatch.setattr(outlook_module.asyncio, "sleep", no_delay)
    original = outlook_module.httpx.AsyncClient
    outlook_module.httpx.AsyncClient = _patched_async_client(httpx.MockTransport(handler))
    try:
        result = asyncio.run(outlook_get_message("token", "message-1"))
    finally:
        outlook_module.httpx.AsyncClient = original

    assert result["id"] == "message-1"
    assert attempts == 2
