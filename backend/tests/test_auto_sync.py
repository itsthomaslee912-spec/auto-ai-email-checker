import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import MailboxConnection, User, WebhookSubscription
from app.services import auto_sync


def test_auto_sync_schedules_only_active_mailboxes_without_webhooks(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        user = User(external_id="auto-sync-test")
        db.add(user)
        db.flush()
        active = MailboxConnection(
            user_id=user.id, provider="google", email_address="active@example.com",
            access_token_enc="x", is_active=True,
        )
        inactive = MailboxConnection(
            user_id=user.id, provider="google", email_address="inactive@example.com",
            access_token_enc="x", is_active=False,
        )
        db.add_all([active, inactive])
        db.flush()
        active_id = active.id
        db.commit()

    sync = AsyncMock()
    monkeypatch.setattr(auto_sync, "SessionLocal", session_factory)
    monkeypatch.setattr(auto_sync, "start_mailbox_sync", sync)
    monkeypatch.setattr(auto_sync, "get_settings", lambda: SimpleNamespace(auto_sync_max_messages=200))

    asyncio.run(auto_sync.poll_active_mailboxes())

    sync.assert_awaited_once_with(active_id, max_results=200, register_webhook=True)
    engine.dispose()


def test_auto_sync_skips_live_webhook_and_polls_expired_one(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        user = User(external_id="webhook-sync-test")
        db.add(user)
        db.flush()
        live = MailboxConnection(user_id=user.id, provider="google", email_address="live@example.com", access_token_enc="x", is_active=True)
        expired = MailboxConnection(user_id=user.id, provider="google", email_address="expired@example.com", access_token_enc="x", is_active=True)
        db.add_all([live, expired])
        db.flush()
        db.add_all([
            WebhookSubscription(mailbox_id=live.id, provider="google", external_id="history-1", expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
            WebhookSubscription(mailbox_id=expired.id, provider="google", external_id="history-2", expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)),
        ])
        live_id, expired_id = live.id, expired.id
        db.commit()

    sync = AsyncMock()
    monkeypatch.setattr(auto_sync, "SessionLocal", session_factory)
    monkeypatch.setattr(auto_sync, "start_mailbox_sync", sync)
    monkeypatch.setattr(auto_sync, "get_settings", lambda: SimpleNamespace(auto_sync_max_messages=200, auto_sync_interval_seconds=120))
    asyncio.run(auto_sync.poll_active_mailboxes())
    sync.assert_awaited_once_with(expired_id, max_results=200, register_webhook=True)
    with session_factory() as db:
        status = auto_sync.get_auto_sync_status(db)
        assert status["webhook_accounts"] == 1
        assert status["connected_accounts"] == 2
    assert live_id != expired_id
    engine.dispose()


def test_auto_sync_status_distinguishes_no_account_stopped_and_failure(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(auto_sync, "get_settings", lambda: SimpleNamespace(auto_sync_interval_seconds=120))
    monkeypatch.setattr(auto_sync, "_loop_running", True)
    monkeypatch.setattr(auto_sync, "_last_check_at", datetime.now(timezone.utc))
    monkeypatch.setattr(auto_sync, "_last_error", None)

    with session_factory() as db:
        assert auto_sync.get_auto_sync_status(db)["state"] == "no_account"
        user = User(external_id="status-test")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id, provider="google", email_address="test@example.com",
            access_token_enc="x", is_active=True,
        )
        db.add(mailbox)
        db.commit()

        monkeypatch.setattr(auto_sync, "get_sync_status", lambda _: {"state": "done", "failed": 0})
        assert auto_sync.get_auto_sync_status(db)["state"] == "active"
        monkeypatch.setattr(auto_sync, "_loop_running", False)
        assert auto_sync.get_auto_sync_status(db)["state"] == "stopped"
        monkeypatch.setattr(auto_sync, "_loop_running", True)
        monkeypatch.setattr(auto_sync, "get_sync_status", lambda _: {"state": "error", "message": "Token expired"})
        status = auto_sync.get_auto_sync_status(db)
        assert status["state"] == "error"
        assert status["problem_mailboxes"][0]["email_address"] == "test@example.com"
    engine.dispose()
