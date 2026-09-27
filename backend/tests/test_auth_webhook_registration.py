import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

from fastapi import BackgroundTasks, HTTPException

from app.api import auth


def test_google_callback_registers_webhook_before_starting_sync(monkeypatch):
    mailbox = SimpleNamespace(
        id=42,
        email_address="person@example.com",
        display_name="Person",
        provider="google",
    )
    register = AsyncMock()
    monkeypatch.setattr(auth, "pop_pending", lambda _state: None)
    monkeypatch.setattr(
        auth, "exchange_google_code", AsyncMock(return_value={"access_token": "access"})
    )
    monkeypatch.setattr(
        auth,
        "fetch_google_userinfo",
        AsyncMock(return_value={"email": "person@example.com"}),
    )
    monkeypatch.setattr(auth, "upsert_google_mailbox", lambda *_args, **_kwargs: mailbox)
    monkeypatch.setattr(auth, "register_mailbox_webhook", register)
    monkeypatch.setattr(
        auth, "get_settings", lambda: SimpleNamespace(frontend_origin="http://localhost:5173")
    )

    tasks = BackgroundTasks()
    response = asyncio.run(
        auth.google_callback(tasks, code="code", state="state", db=object())
    )

    register.assert_awaited_once()
    assert register.await_args.kwargs["access_token"] == "access"
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["webhook"] == ["active"]
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].kwargs == {"register_webhook": False}


def test_google_callback_uses_polling_fallback_when_webhook_registration_fails(monkeypatch):
    mailbox = SimpleNamespace(
        id=7,
        email_address="person@example.com",
        display_name=None,
        provider="google",
    )
    monkeypatch.setattr(auth, "pop_pending", lambda _state: None)
    monkeypatch.setattr(
        auth, "exchange_google_code", AsyncMock(return_value={"access_token": "access"})
    )
    monkeypatch.setattr(
        auth,
        "fetch_google_userinfo",
        AsyncMock(return_value={"email": "person@example.com"}),
    )
    monkeypatch.setattr(auth, "upsert_google_mailbox", lambda *_args, **_kwargs: mailbox)
    monkeypatch.setattr(
        auth,
        "register_mailbox_webhook",
        AsyncMock(side_effect=HTTPException(status_code=400, detail="watch failed")),
    )
    monkeypatch.setattr(
        auth, "get_settings", lambda: SimpleNamespace(frontend_origin="http://localhost:5173")
    )

    tasks = BackgroundTasks()
    response = asyncio.run(
        auth.google_callback(tasks, code="code", state="state", db=object())
    )

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["webhook"] == ["fallback"]
    assert query["detail"] == ["watch failed"]
    assert tasks.tasks[0].kwargs == {"register_webhook": True}
