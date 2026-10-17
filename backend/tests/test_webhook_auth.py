import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import webhooks


class Request:
    def __init__(self, authorization: str = ""):
        self.headers = {"Authorization": authorization} if authorization else {}


def test_gmail_push_requires_configured_audience(monkeypatch):
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(gmail_pubsub_audience="", gmail_pubsub_service_account=""),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks._verify_gmail_push_auth(Request()))
    assert exc.value.status_code == 503


def test_gmail_push_requires_bearer_token(monkeypatch):
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(
            gmail_pubsub_audience="https://example.test/api/webhooks/gmail",
            gmail_pubsub_service_account="",
        ),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks._verify_gmail_push_auth(Request()))
    assert exc.value.status_code == 401


def test_gmail_push_verifies_audience_and_service_account(monkeypatch):
    settings = SimpleNamespace(
        gmail_pubsub_audience="https://example.test/api/webhooks/gmail",
        gmail_pubsub_service_account="push@example.iam.gserviceaccount.com",
    )
    seen = {}

    def verify(token, _request, audience):
        seen.update(token=token, audience=audience)
        return {"email": "push@example.iam.gserviceaccount.com"}

    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    monkeypatch.setattr(webhooks.id_token, "verify_oauth2_token", verify)
    asyncio.run(webhooks._verify_gmail_push_auth(Request("Bearer signed-token")))

    assert seen == {
        "token": "signed-token",
        "audience": "https://example.test/api/webhooks/gmail",
    }


def test_gmail_push_rejects_wrong_service_account(monkeypatch):
    settings = SimpleNamespace(
        gmail_pubsub_audience="https://example.test/api/webhooks/gmail",
        gmail_pubsub_service_account="expected@example.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    monkeypatch.setattr(
        webhooks.id_token,
        "verify_oauth2_token",
        lambda *_args, **_kwargs: {"email": "other@example.iam.gserviceaccount.com"},
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks._verify_gmail_push_auth(Request("Bearer signed-token")))
    assert exc.value.status_code == 403


def test_gmail_push_reuses_verified_token_until_expiry(monkeypatch):
    calls = []
    webhooks._gmail_token_cache.clear()

    def verify(token, _request, audience):
        calls.append((token, audience))
        return {
            "email": "push@example.iam.gserviceaccount.com",
            "exp": time.time() + 600,
        }

    monkeypatch.setattr(webhooks.id_token, "verify_oauth2_token", verify)
    args = (
        "signed-token",
        "https://example.test/api/webhooks/gmail",
        "push@example.iam.gserviceaccount.com",
    )

    assert webhooks._verify_gmail_token_cached(*args)["email"].startswith("push@")
    assert webhooks._verify_gmail_token_cached(*args)["email"].startswith("push@")
    assert calls == [(args[0], args[1])]
    webhooks._gmail_token_cache.clear()
