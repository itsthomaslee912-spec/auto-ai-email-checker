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
from app.models import MailboxConnection, Provider, User
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


def test_gmail_webhook_classifies_new_messages_with_active_ai_settings(monkeypatch):
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

    created = asyncio.run(
        mailbox_sync.process_gmail_notification(db, mailbox.email_address, "200")
    )

    assert created == 1
    assert ingest.await_count == 1
    assert ingest.await_args.kwargs == {}
    assert mailbox.sync_cursor == "200"
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
    monkeypatch.setattr(mailbox_sync, "outlook_get_message", AsyncMock(return_value={"id": "new"}))
    monkeypatch.setattr(mailbox_sync, "normalize_outlook_message", lambda raw: raw)
    ingest = AsyncMock(return_value=object())
    monkeypatch.setattr(mailbox_sync, "ingest_normalized_message", ingest)
    asyncio.run(mailbox_sync._incremental_outlook(db, mailbox))
    assert ingest.await_count == 1
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
