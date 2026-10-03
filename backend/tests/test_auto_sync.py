import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import MailboxConnection, User, WebhookSubscription
from app.services import auto_sync
from app.services.webhook_health import (
    record_webhook_delivery_failure,
    record_webhook_delivery_success,
    reset_webhook_delivery_health,
)

NGROK_URL = "https://example.ngrok-free.dev"


@pytest.fixture(autouse=True)
def ngrok_online(monkeypatch):
    status = {"state": "online", "public_url": NGROK_URL, "expected_url": NGROK_URL}
    monkeypatch.setattr(auto_sync, "get_ngrok_status", lambda: status)
    return status


def _verified_mailbox(session_factory, delivered_ago: timedelta) -> int:
    with session_factory() as db:
        user = User(external_id="verified-webhook")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id, provider="google", email_address="verified@example.com",
            access_token_enc="x", is_active=True,
        )
        db.add(mailbox)
        db.flush()
        db.add(WebhookSubscription(
            mailbox_id=mailbox.id, provider="google", external_id="history-1",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            last_delivery_success_at=datetime.now(timezone.utc) - delivered_ago,
        ))
        db.commit()
        return mailbox.id


def _poll(monkeypatch, session_factory) -> AsyncMock:
    sync = AsyncMock()
    monkeypatch.setattr(auto_sync, "SessionLocal", session_factory)
    monkeypatch.setattr(auto_sync, "start_mailbox_sync", sync)
    monkeypatch.setattr(auto_sync, "get_settings", lambda: SimpleNamespace(
        auto_sync_max_messages=200, auto_sync_interval_seconds=120,
    ))
    asyncio.run(auto_sync.poll_active_mailboxes())
    return sync


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def test_recent_verified_webhook_keeps_polling_off(monkeypatch):
    engine, session_factory = _session_factory()
    _verified_mailbox(session_factory, timedelta(minutes=5))

    sync = _poll(monkeypatch, session_factory)

    sync.assert_not_awaited()
    with session_factory() as db:
        status = auto_sync.get_auto_sync_status(db)
        assert status["polling_accounts"] == 0
        assert status["mailboxes"][0]["sync_method"] == "webhook"
    engine.dispose()


@pytest.mark.parametrize("state", ["offline", "mismatch"])
def test_polling_turns_on_when_ngrok_cannot_deliver(monkeypatch, ngrok_online, state):
    engine, session_factory = _session_factory()
    mailbox_id = _verified_mailbox(session_factory, timedelta(minutes=5))
    ngrok_online["state"] = state

    sync = _poll(monkeypatch, session_factory)

    sync.assert_awaited_once_with(mailbox_id, max_results=200, register_webhook=True)
    with session_factory() as db:
        status = auto_sync.get_auto_sync_status(db)
        assert status["polling_accounts"] == 1
        assert status["mailboxes"][0]["sync_method"] == "polling"
        assert "ngrok" in status["mailboxes"][0]["polling_reason"]
    engine.dispose()


def test_ngrok_state_is_ignored_when_webhook_url_is_not_ngrok(monkeypatch, ngrok_online):
    engine, session_factory = _session_factory()
    _verified_mailbox(session_factory, timedelta(minutes=5))
    ngrok_online.update(state="offline", expected_url="https://mail.example.com")

    sync = _poll(monkeypatch, session_factory)

    sync.assert_not_awaited()
    engine.dispose()


def test_quiet_inbox_stays_on_webhook(monkeypatch):
    engine, session_factory = _session_factory()
    _verified_mailbox(session_factory, timedelta(hours=2))

    sync = _poll(monkeypatch, session_factory)

    sync.assert_not_awaited()
    with session_factory() as db:
        detail = auto_sync.get_auto_sync_status(db)["mailboxes"][0]
        assert detail["sync_method"] == "webhook"
        assert detail["polling_reason"] is None
        assert auto_sync.get_auto_sync_status(db)["polling_accounts"] == 0
    engine.dispose()


def test_polling_waits_for_reconnect_when_sign_in_expired(monkeypatch):
    engine, session_factory = _session_factory()
    mailbox_id = _verified_mailbox(session_factory, timedelta(hours=2))
    with session_factory() as db:
        db.get(MailboxConnection, mailbox_id).last_sync_error = (
            "401: Google sign-in expired or was revoked; reconnect this account"
        )
        db.commit()

    sync = _poll(monkeypatch, session_factory)

    sync.assert_not_awaited()
    with session_factory() as db:
        assert auto_sync.get_auto_sync_status(db)["mailboxes"][0]["needs_reconnect"] is True
    engine.dispose()


def test_auto_sync_schedules_every_active_mailbox(monkeypatch):
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


def test_auto_sync_reconciles_live_and_expired_webhooks(monkeypatch):
    reset_webhook_delivery_health()
    record_webhook_delivery_success("google")
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
            WebhookSubscription(
                mailbox_id=live.id,
                provider="google",
                external_id="history-1",
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                last_delivery_success_at=datetime.now(timezone.utc),
            ),
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
    reset_webhook_delivery_health()


def test_auto_sync_reconciles_verified_webhook_after_process_restart(monkeypatch):
    reset_webhook_delivery_health()
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        user = User(external_id="persisted-webhook-test")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider="google",
            email_address="persisted@example.com",
            access_token_enc="x",
            is_active=True,
        )
        db.add(mailbox)
        db.flush()
        db.add(WebhookSubscription(
            mailbox_id=mailbox.id,
            provider="google",
            external_id="history-persisted",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            last_delivery_success_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ))
        mailbox_id = mailbox.id
        db.commit()

    sync = AsyncMock()
    monkeypatch.setattr(auto_sync, "SessionLocal", session_factory)
    monkeypatch.setattr(auto_sync, "start_mailbox_sync", sync)
    monkeypatch.setattr(
        auto_sync,
        "get_settings",
        lambda: SimpleNamespace(
            auto_sync_max_messages=200, auto_sync_interval_seconds=120
        ),
    )

    asyncio.run(auto_sync.poll_active_mailboxes())

    sync.assert_not_awaited()
    with session_factory() as db:
        mailbox = db.get(MailboxConnection, mailbox_id)
        assert auto_sync.has_active_webhook(mailbox)
        status = auto_sync.get_auto_sync_status(db)
        assert status["webhook_accounts"] == 1
        assert status["webhook_delivery"]["google"]["healthy"] is True
    engine.dispose()


def test_auto_sync_polls_registered_webhook_after_delivery_auth_failure(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        user = User(external_id="webhook-fallback-test")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider="google",
            email_address="fallback@example.com",
            access_token_enc="x",
            is_active=True,
        )
        db.add(mailbox)
        db.flush()
        db.add(
            WebhookSubscription(
                mailbox_id=mailbox.id,
                provider="google",
                external_id="history-1",
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        mailbox_id = mailbox.id
        db.commit()

    reset_webhook_delivery_health()
    record_webhook_delivery_success("google")
    record_webhook_delivery_failure("google", "Missing Pub/Sub bearer token")
    sync = AsyncMock()
    monkeypatch.setattr(auto_sync, "SessionLocal", session_factory)
    monkeypatch.setattr(auto_sync, "start_mailbox_sync", sync)
    monkeypatch.setattr(
        auto_sync,
        "get_settings",
        lambda: SimpleNamespace(
            auto_sync_max_messages=200, auto_sync_interval_seconds=120
        ),
    )

    asyncio.run(auto_sync.poll_active_mailboxes())

    sync.assert_awaited_once_with(
        mailbox_id, max_results=200, register_webhook=True
    )
    with session_factory() as db:
        status = auto_sync.get_auto_sync_status(db)
        assert status["webhook_accounts"] == 0
        assert status["registered_webhook_accounts"] == 1
        assert status["webhook_delivery"]["google"]["last_error"] == (
            "Missing Pub/Sub bearer token"
        )
    reset_webhook_delivery_health()
    engine.dispose()


def test_webhook_health_is_isolated_per_mailbox():
    reset_webhook_delivery_health()
    record_webhook_delivery_success("microsoft")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        user = User(external_id="isolated-webhook-health")
        db.add(user)
        db.flush()
        verified = MailboxConnection(
            user_id=user.id, provider="microsoft", email_address="verified@example.com",
            access_token_enc="x", is_active=True,
        )
        unverified = MailboxConnection(
            user_id=user.id, provider="microsoft", email_address="unverified@example.com",
            access_token_enc="x", is_active=True,
        )
        db.add_all([verified, unverified])
        db.flush()
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        db.add_all([
            WebhookSubscription(
                mailbox_id=verified.id, provider="microsoft", external_id="sub-1",
                expires_at=expires, last_delivery_success_at=datetime.now(timezone.utc),
            ),
            WebhookSubscription(
                mailbox_id=unverified.id, provider="microsoft", external_id="sub-2",
                expires_at=expires,
            ),
        ])
        db.commit()

        assert auto_sync.has_active_webhook(verified)
        assert not auto_sync.has_active_webhook(unverified)
        assert auto_sync.get_auto_sync_status(db)["webhook_accounts"] == 1
    reset_webhook_delivery_health()
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


def _mailbox_with_webhook(session_factory, *, success_ago: timedelta, failure_ago: timedelta | None, error: str | None):
    with session_factory() as db:
        user = User(external_id="quota-webhook")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id, provider="google", email_address="thomas@example.com",
            access_token_enc="x", is_active=True, last_sync_error=error,
        )
        db.add(mailbox)
        db.flush()
        now = datetime.now(timezone.utc)
        db.add(WebhookSubscription(
            mailbox_id=mailbox.id, provider="google", external_id="history-1",
            expires_at=now + timedelta(days=1),
            last_delivery_success_at=now - success_ago,
            last_delivery_failure_at=None if failure_ago is None else now - failure_ago,
            last_delivery_error=error,
        ))
        mailbox_id = mailbox.id
        db.commit()
        return mailbox_id


def test_auto_sync_keeps_webhook_after_a_fresh_gmail_quota_failure(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    quota = "Gmail get message failed: Quota exceeded for quota metric 'Total Query Cost'"
    mailbox_id = _mailbox_with_webhook(
        session_factory, success_ago=timedelta(hours=2), failure_ago=timedelta(minutes=1), error=quota,
    )
    sync = _poll(monkeypatch, session_factory)

    sync.assert_not_awaited()
    with session_factory() as db:
        mailbox = db.get(MailboxConnection, mailbox_id)
        assert auto_sync.polling_reason(mailbox) is None
        assert auto_sync.has_active_webhook(mailbox)
        status = auto_sync.get_auto_sync_status(db)
        detail = status["mailboxes"][0]
        assert detail["sync_method"] == "webhook"
        assert detail["last_error"] is None
        assert status["polling_accounts"] == 0
        assert status["problem_mailboxes"] == []
    engine.dispose()


def test_gmail_quota_holdoff_expiry_stays_on_webhook(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    quota = "Gmail get message failed: Quota exceeded"
    mailbox_id = _mailbox_with_webhook(
        session_factory, success_ago=timedelta(hours=2), failure_ago=timedelta(minutes=20), error=quota,
    )
    sync = _poll(monkeypatch, session_factory)

    sync.assert_not_awaited()
    with session_factory() as db:
        mailbox = db.get(MailboxConnection, mailbox_id)
        assert auto_sync.polling_reason(mailbox) is None
        assert auto_sync.get_auto_sync_status(db)["mailboxes"][0]["sync_method"] == "webhook"
    engine.dispose()
