from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.email import gmail
from app.models import EmailLabel, EmailMessage, MailboxConnection, Provider, User
from app.realtime import gmail_watch
from app.services import mailbox_sync


def _mailbox(provider: Provider):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(external_id=f"incremental-{provider.value}")
    db.add(user)
    db.flush()
    mailbox = MailboxConnection(
        user_id=user.id, provider=provider.value, email_address=f"{provider.value}@example.com",
        access_token_enc="x", is_active=True, full_sync_completed=True,
    )
    db.add(mailbox)
    db.commit()
    return engine, db, mailbox


def test_gmail_history_reads_all_pages_and_preserves_expired_signal(monkeypatch):
    responses = [
        (200, {"history": [{"id": "101"}], "nextPageToken": "page-2", "historyId": "200"}),
        (200, {"history": [{"id": "102"}], "historyId": "201"}),
    ]
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, params, **_kwargs):
            calls.append(params)
            status, data = responses.pop(0)
            return SimpleNamespace(status_code=status, json=lambda: data, text="expired")

    monkeypatch.setattr(gmail.httpx, "AsyncClient", lambda **_kwargs: Client())
    result = asyncio.run(gmail.gmail_list_history("token", "100"))
    assert [row["id"] for row in result["history"]] == ["101", "102"]
    assert result["historyId"] == "201"
    assert calls[1]["pageToken"] == "page-2"

    responses.append((404, {}))
    with pytest.raises(gmail.GmailHistoryExpired):
        asyncio.run(gmail.gmail_list_history("token", "100"))


def test_gmail_history_retries_a_dropped_connection(monkeypatch):
    """A keep-alive connection the server closes mid-race must not abort the sync.

    httpx raises RemoteProtocolError ("Server disconnected without sending a
    response") with no Response to inspect, unlike a quota/5xx reply. Before
    this was handled, one dropped connection during webhook notification
    processing flipped the account's webhook to "failed" and fell back to
    polling.
    """
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, params, **_kwargs):
            calls.append(params)
            if len(calls) == 1:
                raise gmail.httpx.RemoteProtocolError("Server disconnected without sending a response")
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"history": [{"id": "101"}], "historyId": "200"},
                text="",
            )

    monkeypatch.setattr(gmail.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(gmail.asyncio, "sleep", AsyncMock())
    result = asyncio.run(gmail.gmail_list_history("token", "100"))
    assert len(calls) == 2
    assert [row["id"] for row in result["history"]] == ["101"]


def test_gmail_history_reraises_after_exhausting_retries_on_dropped_connections(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, params, **_kwargs):
            raise gmail.httpx.RemoteProtocolError("Server disconnected without sending a response")

    monkeypatch.setattr(gmail.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(gmail.asyncio, "sleep", AsyncMock())
    with pytest.raises(gmail.httpx.RemoteProtocolError):
        asyncio.run(gmail.gmail_list_history("token", "100"))


def test_gmail_incremental_advances_cursor_only_after_fetch_and_ingest(monkeypatch):
    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    mailbox.sync_cursor = "100"
    db.commit()
    monkeypatch.setattr(mailbox_sync, "ensure_google_access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(mailbox_sync, "_publish_progress", AsyncMock())
    monkeypatch.setattr(mailbox_sync, "gmail_list_history", AsyncMock(return_value={
        "historyId": "200",
        "history": [{"messagesAdded": [{"message": {"id": "new"}}]},
                    {"labelsAdded": [{"message": {"id": "new"}}]}],
    }))
    get_messages = AsyncMock(return_value=[{"id": "new", "labelIds": ["INBOX"]}])
    monkeypatch.setattr(mailbox_sync, "gmail_get_messages", get_messages)
    monkeypatch.setattr(mailbox_sync, "normalize_gmail_message", lambda raw: raw)
    ingest = AsyncMock(return_value=object())
    monkeypatch.setattr(mailbox_sync, "ingest_normalized_message", ingest)

    asyncio.run(mailbox_sync._incremental_gmail(db, mailbox))
    assert mailbox.sync_cursor == "200"
    assert get_messages.await_args.args[1] == ["new"]
    assert ingest.await_count == 1
    assert ingest.await_args.kwargs["download_only"] is True

    mailbox.sync_cursor = "200"
    db.commit()
    get_messages.return_value = []
    monkeypatch.setattr(mailbox_sync, "gmail_get_message", AsyncMock(side_effect=HTTPException(503)))
    with pytest.raises(HTTPException):
        asyncio.run(mailbox_sync._incremental_gmail(db, mailbox))
    db.refresh(mailbox)
    assert mailbox.sync_cursor == "200"
    db.close()
    engine.dispose()


def test_gmail_webhook_commits_before_starting_background_classification(monkeypatch):
    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    mailbox.sync_cursor = "100"
    db.commit()
    monkeypatch.setattr(mailbox_sync, "ensure_google_access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(mailbox_sync, "gmail_list_history", AsyncMock(return_value={
        "historyId": "200",
        "history": [{"messagesAdded": [{"message": {"id": "new"}}]}],
    }))
    monkeypatch.setattr(
        mailbox_sync,
        "gmail_get_message",
        AsyncMock(return_value={"id": "new", "labelIds": ["INBOX"]}),
    )
    monkeypatch.setattr(mailbox_sync, "normalize_gmail_message", lambda raw: raw)
    ingest = AsyncMock(return_value=object())
    monkeypatch.setattr(mailbox_sync, "ingest_normalized_message", ingest)
    classify_downloaded = AsyncMock()
    monkeypatch.setattr(mailbox_sync, "_classify_downloaded_mail", classify_downloaded)

    created = asyncio.run(
        mailbox_sync.process_gmail_notification(db, mailbox.email_address, "200")
    )

    assert created == 1
    assert ingest.await_count == 1
    assert ingest.await_args.kwargs == {"download_only": True}
    classify_downloaded.assert_awaited_once_with(mailbox.id)
    assert mailbox.sync_cursor == "200"
    db.close()
    engine.dispose()


def test_gmail_quota_failure_keeps_the_webhook(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.models import WebhookSubscription

    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    mailbox.sync_cursor = "100"
    db.add(WebhookSubscription(
        mailbox_id=mailbox.id, provider="google", external_id="watch",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        last_delivery_success_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    ))
    db.commit()
    monkeypatch.setattr(mailbox_sync, "ensure_google_access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(mailbox_sync, "gmail_list_history", AsyncMock(return_value={
        "historyId": "200",
        "history": [{"messagesAdded": [{"message": {"id": "new"}}]}],
    }))
    monkeypatch.setattr(
        mailbox_sync, "gmail_get_message",
        AsyncMock(side_effect=HTTPException(400, "Gmail get message failed: Quota exceeded for quota metric")),
    )

    with pytest.raises(HTTPException):
        asyncio.run(mailbox_sync.process_gmail_notification(db, mailbox.email_address, "200"))

    db.refresh(mailbox)
    assert mailbox.last_sync_error is None
    assert mailbox.sync_cursor == "100"
    assert mailbox.webhook.last_delivery_failure_at is None
    assert mailbox.webhook.last_delivery_success_at is not None
    db.close()
    engine.dispose()


def test_gmail_get_message_retries_quota_then_returns_the_message(monkeypatch):
    import httpx

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, text='{"error":{"message":"Quota exceeded"}}')
        return httpx.Response(200, json={"id": "m1", "payload": {}})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client(*_args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*_args, **kwargs)

    monkeypatch.setattr(gmail.httpx, "AsyncClient", client)
    monkeypatch.setattr(gmail.asyncio, "sleep", AsyncMock())

    raw = asyncio.run(gmail.gmail_get_message("token", "m1"))

    assert raw["id"] == "m1"
    assert calls["n"] == 2


def test_gmail_webhook_failure_marks_mailbox_webhook_unhealthy(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.models import WebhookSubscription
    from app.services.auto_sync import has_active_webhook

    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    mailbox.sync_cursor = "100"
    db.add(WebhookSubscription(
        mailbox_id=mailbox.id, provider="google", external_id="watch",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        last_delivery_success_at=datetime.now(timezone.utc) - timedelta(hours=1),
    ))
    db.commit()
    db.refresh(mailbox)
    assert has_active_webhook(mailbox)
    monkeypatch.setattr(
        mailbox_sync, "ensure_google_access_token",
        AsyncMock(side_effect=HTTPException(401, "Google sign-in expired or was revoked")),
    )

    with pytest.raises(HTTPException):
        asyncio.run(mailbox_sync.process_gmail_notification(db, mailbox.email_address, "200"))

    db.refresh(mailbox)
    assert mailbox.last_sync_error == "Google sign-in expired or was revoked"
    assert mailbox.webhook.last_delivery_error == "Google sign-in expired or was revoked"
    assert not has_active_webhook(mailbox)
    db.close()
    engine.dispose()


def test_reconnect_clears_sign_in_errors_but_keeps_other_failures():
    from datetime import datetime, timezone

    from app.models import WebhookSubscription
    from app.services.reconnect import clear_reconnect_errors, needs_reconnect

    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    expired = "401: Google sign-in expired or was revoked; reconnect this account"
    mailbox.last_sync_error = expired
    db.add(WebhookSubscription(
        mailbox_id=mailbox.id, provider="google", external_id="watch",
        last_delivery_failure_at=datetime.now(timezone.utc), last_delivery_error=expired,
    ))
    db.commit()
    db.refresh(mailbox)
    assert needs_reconnect(mailbox.last_sync_error)

    clear_reconnect_errors(mailbox)
    assert mailbox.last_sync_error is None
    assert mailbox.webhook.last_delivery_failure_at is None

    mailbox.last_sync_error = "Gmail API 503"
    clear_reconnect_errors(mailbox)
    assert mailbox.last_sync_error == "Gmail API 503"
    assert not needs_reconnect("Gmail API 503")
    db.close()
    engine.dispose()


def test_gmail_webhook_does_not_fall_back_to_another_mailbox(monkeypatch):
    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    ensure_token = AsyncMock(return_value="token")
    list_history = AsyncMock(return_value={"historyId": "200", "history": []})
    monkeypatch.setattr(mailbox_sync, "ensure_google_access_token", ensure_token)
    monkeypatch.setattr(mailbox_sync, "gmail_list_history", list_history)

    created = asyncio.run(
        mailbox_sync.process_gmail_notification(db, "unknown@example.com", "200")
    )

    assert created == 0
    ensure_token.assert_not_awaited()
    list_history.assert_not_awaited()
    db.close()
    engine.dispose()


def test_sender_exclusion_is_scoped_to_configured_mailbox(monkeypatch):
    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    mailbox.email_address = "Ryan@Example.com"
    monkeypatch.setattr(
        mailbox_sync,
        "get_settings",
        lambda: SimpleNamespace(
            mail_sync_excluded_senders='{"ryan@example.com":["thomas@example.com"]}'
        ),
    )

    assert mailbox_sync._message_sender_is_excluded(
        mailbox, {"sender": "Thomas Lee <THOMAS@example.com>"}
    )
    assert not mailbox_sync._message_sender_is_excluded(
        mailbox, {"sender": "Someone Else <someone@example.com>"}
    )
    mailbox.email_address = "jordan@example.com"
    assert not mailbox_sync._message_sender_is_excluded(
        mailbox, {"sender": "Thomas Lee <thomas@example.com>"}
    )
    db.close()
    engine.dispose()


def test_gmail_message_must_be_addressed_or_delivered_to_its_mailbox():
    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    mailbox.email_address = "ryan@example.com"

    assert mailbox_sync._gmail_message_belongs_to_mailbox(mailbox, {
        "payload": {"headers": [{"name": "To", "value": "Ryan <ryan@example.com>"}]}
    })
    assert mailbox_sync._gmail_message_belongs_to_mailbox(mailbox, {
        "payload": {"headers": [
            {"name": "To", "value": "A mailing list <jobs@example.com>"},
            {"name": "Delivered-To", "value": "ryan@example.com"},
        ]}
    })
    assert not mailbox_sync._gmail_message_belongs_to_mailbox(mailbox, {
        "payload": {"headers": [
            {"name": "From", "value": "Thomas <thomas@example.com>"},
            {"name": "To", "value": "Recruiter <recruiter@example.com>"},
        ]}
    })
    # Sent replies are From the connected account, not To it.
    assert mailbox_sync._gmail_message_belongs_to_mailbox(mailbox, {
        "labelIds": ["SENT"],
        "payload": {"headers": [
            {"name": "From", "value": "Ryan <ryan@example.com>"},
            {"name": "To", "value": "Recruiter <recruiter@example.com>"},
        ]}
    })
    assert not mailbox_sync._gmail_message_belongs_to_mailbox(mailbox, {
        "labelIds": ["SENT"],
        "payload": {"headers": [
            {"name": "From", "value": "Thomas <thomas@example.com>"},
            {"name": "To", "value": "Recruiter <recruiter@example.com>"},
        ]}
    })
    db.close()
    engine.dispose()


def test_outlook_delta_tracks_each_folder_and_keeps_cursor_on_failure(monkeypatch):
    engine, db, mailbox = _mailbox(Provider.MICROSOFT)
    old = {folder: f"https://graph.microsoft.com/v1.0/{folder}/old" for folder in mailbox_sync.OUTLOOK_FOLDERS}
    mailbox.sync_cursor = json.dumps(old)
    db.commit()
    monkeypatch.setattr(mailbox_sync, "ensure_microsoft_access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(mailbox_sync, "_publish_progress", AsyncMock())
    pages = {
        "inbox": [
            {"value": [{"id": "new"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/inbox/next"},
            {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/inbox/done"},
        ],
        **{folder: [{"value": [], "@odata.deltaLink": f"https://graph.microsoft.com/v1.0/{folder}/done"}]
           for folder in mailbox_sync.OUTLOOK_FOLDERS if folder != "inbox"},
    }

    async def delta(_token, folder, _cursor):
        return pages[folder].pop(0)

    monkeypatch.setattr(mailbox_sync, "outlook_list_delta", delta)
    get_message = AsyncMock(return_value={"id": "new"})
    monkeypatch.setattr(mailbox_sync, "outlook_get_message", get_message)
    monkeypatch.setattr(mailbox_sync, "normalize_outlook_message", lambda raw: raw)
    ingest = AsyncMock(return_value=object())
    monkeypatch.setattr(mailbox_sync, "ingest_normalized_message", ingest)
    asyncio.run(mailbox_sync._incremental_outlook(db, mailbox))
    assert ingest.await_count == 1
    assert get_message.await_count == 1
    assert get_message.await_args.kwargs.get("client") is not None
    assert set(json.loads(mailbox.sync_cursor)) == set(mailbox_sync.OUTLOOK_FOLDERS)
    assert json.loads(mailbox.sync_cursor)["inbox"].endswith("/done")

    saved = mailbox.sync_cursor
    monkeypatch.setattr(mailbox_sync, "outlook_list_delta", AsyncMock(side_effect=RuntimeError("network")))
    with pytest.raises(RuntimeError):
        asyncio.run(mailbox_sync._incremental_outlook(db, mailbox))
    db.refresh(mailbox)
    assert mailbox.sync_cursor == saved
    db.close()
    engine.dispose()


def test_sync_uses_saved_cursor_and_full_rescan_bypasses_it(monkeypatch):
    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    mailbox.sync_cursor = "100"
    db.commit()
    incremental = AsyncMock()
    full = AsyncMock()
    monkeypatch.setattr(mailbox_sync, "_incremental_gmail", incremental)
    monkeypatch.setattr(mailbox_sync, "_full_gmail", full)
    monkeypatch.setattr(mailbox_sync, "get_settings", lambda: SimpleNamespace(mail_sync_max=500))

    asyncio.run(mailbox_sync.bootstrap_mailbox(db, mailbox, register_webhook=False))
    incremental.assert_awaited_once()
    full.assert_not_awaited()

    asyncio.run(mailbox_sync.bootstrap_mailbox(db, mailbox, register_webhook=False, force_full=True))
    full.assert_awaited_once_with(db, mailbox, 500)
    db.close()
    engine.dispose()


def test_full_gmail_refresh_set_includes_all_existing_interview_messages_only():
    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    rows = [
        EmailMessage(mailbox_id=mailbox.id, provider_message_id="scheduled", subject="Scheduled", sender="a@example.com", label=EmailLabel.INTERVIEW_SCHEDULED.value),
        EmailMessage(mailbox_id=mailbox.id, provider_message_id="invitation", subject="Invitation", sender="a@example.com", label=EmailLabel.INTERVIEW_INVITATION.value),
        EmailMessage(mailbox_id=mailbox.id, provider_message_id="outside-list-window", subject="Older scheduled", sender="a@example.com", label=EmailLabel.INTERVIEW_SCHEDULED.value),
        EmailMessage(mailbox_id=mailbox.id, provider_message_id="other", subject="Other", sender="a@example.com", label=EmailLabel.OTHER.value),
    ]
    db.add_all(rows)
    db.commit()

    assert mailbox_sync._existing_calendar_ids(db, mailbox.id) == ["scheduled", "invitation", "outside-list-window"]
    db.close()
    engine.dispose()


def test_gmail_watch_renewal_keeps_unprocessed_history_cursor(monkeypatch):
    engine, db, mailbox = _mailbox(Provider.GOOGLE)
    mailbox.sync_cursor = "100"
    db.commit()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return SimpleNamespace(
                status_code=200, json=lambda: {"historyId": "200", "expiration": "1800000000000"},
            )

    monkeypatch.setattr(gmail_watch.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(gmail_watch, "get_settings", lambda: SimpleNamespace(gmail_pubsub_topic="projects/p/topics/t"))
    asyncio.run(gmail_watch.start_gmail_watch(db, mailbox, "token"))
    db.refresh(mailbox)
    assert mailbox.sync_cursor == "100"
    db.close()
    engine.dispose()


def test_outlook_notification_never_falls_back_to_another_mailbox(monkeypatch):
    engine, db, _mailbox_row = _mailbox(Provider.MICROSOFT)
    token = AsyncMock(return_value="token")
    get_message = AsyncMock()
    monkeypatch.setattr(mailbox_sync, "ensure_microsoft_access_token", token)
    monkeypatch.setattr(mailbox_sync, "outlook_get_message", get_message)

    created = asyncio.run(
        mailbox_sync.process_outlook_notification(db, "unknown-subscription", "message-1")
    )

    assert created == 0
    token.assert_not_awaited()
    get_message.assert_not_awaited()
    db.close()
    engine.dispose()


def test_bulk_mailbox_sync_jobs_are_serialized(monkeypatch):
    active = 0
    peak = 0

    async def fake_sync(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    async def run_two():
        mailbox_sync._sync_gate = None
        try:
            await asyncio.gather(
                mailbox_sync._run_sync_job(1),
                mailbox_sync._run_sync_job(2),
            )
        finally:
            mailbox_sync._sync_gate = None

    monkeypatch.setattr(mailbox_sync, "_run_sync_job_exclusive", fake_sync)
    asyncio.run(run_two())

    assert peak == 1
