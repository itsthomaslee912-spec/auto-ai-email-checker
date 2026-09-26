from __future__ import annotations

from unittest.mock import AsyncMock, patch
import asyncio

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import emails
from app.db import Base, get_db
from app.models import EmailLabel, EmailMessage, MailboxConnection, Provider, User


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _client(SessionLocal):
    app = FastAPI()
    app.include_router(emails.router)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _seed_unread_email(db) -> EmailMessage:
    user = User(external_id="u-mark-read")
    db.add(user)
    db.flush()
    mailbox = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address="mark-read@example.com",
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    db.add(mailbox)
    db.flush()
    email = EmailMessage(
        mailbox_id=mailbox.id,
        provider_message_id="msg-unread-1",
        subject="Please open me",
        sender="sender@example.com",
        snippet="hello",
        body_text="Hello body",
        label=EmailLabel.OTHER.value,
        is_read=False,
    )
    db.add(email)
    db.commit()
    db.refresh(email)
    return email


def test_get_email_marks_provider_read_once():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_unread_email(db)
    email_id = email.id
    db.close()

    client = _client(SessionLocal)
    mark_read = AsyncMock()
    ensure_token = AsyncMock(return_value="token")

    with (
        patch("app.api.emails.gmail_mark_read", mark_read),
        patch("app.api.emails.ensure_google_access_token", ensure_token),
        patch("app.api.emails.SessionLocal", SessionLocal),
    ):
        first = client.get(f"/api/emails/{email_id}")
        assert first.status_code == 200
        assert first.json()["is_read"] is True
        mark_read.assert_awaited_once_with("token", "msg-unread-1")

        mark_read.reset_mock()
        second = client.get(f"/api/emails/{email_id}")
        assert second.status_code == 200
        assert second.json()["is_read"] is True
        mark_read.assert_not_awaited()


def test_get_email_returns_before_provider_read():
    SessionLocal = _session_factory()
    with SessionLocal() as db:
        email = _seed_unread_email(db)
        email_id = email.id

    tasks = BackgroundTasks()
    mark_read = AsyncMock()
    with patch("app.api.emails.gmail_mark_read", mark_read):
        with SessionLocal() as db:
            result = asyncio.run(emails.get_email(email_id, tasks, db))
            assert result.is_read is True
            assert result.body_text == "Hello body"
        mark_read.assert_not_awaited()
        assert len(tasks.tasks) == 1


def _seed_mailbox(db, *, external_id: str, email_address: str) -> MailboxConnection:
    user = db.query(User).filter(User.external_id == external_id).one_or_none()
    if user is None:
        user = User(external_id=external_id)
        db.add(user)
        db.flush()
    mailbox = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address=email_address,
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    db.add(mailbox)
    db.flush()
    return mailbox


def _add_email(db, mailbox: MailboxConnection, provider_message_id: str, *, is_read: bool) -> EmailMessage:
    email = EmailMessage(
        mailbox_id=mailbox.id,
        provider_message_id=provider_message_id,
        subject=provider_message_id,
        sender="sender@example.com",
        snippet="hello",
        body_text="Hello body",
        label=EmailLabel.OTHER.value,
        is_read=is_read,
    )
    db.add(email)
    return email


def test_mark_all_read_includes_every_account_unread():
    SessionLocal = _session_factory()
    db = SessionLocal()
    first = _seed_mailbox(db, external_id="u-all", email_address="a@example.com")
    second = _seed_mailbox(db, external_id="u-all", email_address="b@example.com")
    _add_email(db, first, "a-unread", is_read=False)
    _add_email(db, first, "a-read", is_read=True)
    _add_email(db, second, "b-unread", is_read=False)
    db.commit()
    db.close()

    client = _client(SessionLocal)
    mark_many = AsyncMock()
    ensure_token = AsyncMock(return_value="token")

    with (
        patch("app.api.emails.gmail_mark_read_many", mark_many),
        patch("app.api.emails.ensure_google_access_token", ensure_token),
    ):
        res = client.post("/api/emails/mark-all-read")
        assert res.status_code == 200
        assert res.json()["marked"] == 2

    sent = sorted(message_id for call in mark_many.await_args_list for message_id in call.args[1])
    assert sent == ["a-unread", "b-unread"]

    db = SessionLocal()
    rows = {row.provider_message_id: row.is_read for row in db.query(EmailMessage).all()}
    db.close()
    assert rows == {"a-unread": True, "a-read": True, "b-unread": True}


def test_mark_all_read_limits_to_one_mailbox():
    SessionLocal = _session_factory()
    db = SessionLocal()
    first = _seed_mailbox(db, external_id="u-one", email_address="a@example.com")
    second = _seed_mailbox(db, external_id="u-one", email_address="b@example.com")
    _add_email(db, first, "a-unread", is_read=False)
    _add_email(db, second, "b-unread", is_read=False)
    mailbox_id = first.id
    db.commit()
    db.close()

    client = _client(SessionLocal)
    mark_many = AsyncMock()
    ensure_token = AsyncMock(return_value="token")

    with (
        patch("app.api.emails.gmail_mark_read_many", mark_many),
        patch("app.api.emails.ensure_google_access_token", ensure_token),
    ):
        res = client.post("/api/emails/mark-all-read", params={"mailbox_id": mailbox_id})
        assert res.status_code == 200
        assert res.json()["marked"] == 1
        mark_many.assert_awaited_once_with("token", ["a-unread"])

    db = SessionLocal()
    rows = {row.provider_message_id: row.is_read for row in db.query(EmailMessage).all()}
    db.close()
    assert rows["a-unread"] is True
    assert rows["b-unread"] is False


def test_mark_all_read_keeps_local_read_when_provider_fails():
    SessionLocal = _session_factory()
    db = SessionLocal()
    mailbox = _seed_mailbox(db, external_id="u-fail", email_address="a@example.com")
    _add_email(db, mailbox, "a-unread", is_read=False)
    db.commit()
    db.close()

    client = _client(SessionLocal)
    mark_many = AsyncMock(side_effect=RuntimeError("quota"))
    ensure_token = AsyncMock(return_value="token")

    with (
        patch("app.api.emails.gmail_mark_read_many", mark_many),
        patch("app.api.emails.ensure_google_access_token", ensure_token),
    ):
        res = client.post("/api/emails/mark-all-read")
        assert res.status_code == 200
        assert res.json()["marked"] == 1
        mark_many.assert_awaited_once()

    db = SessionLocal()
    stored = db.query(EmailMessage).filter(EmailMessage.provider_message_id == "a-unread").one()
    assert stored.is_read is True
    db.close()


def test_get_email_still_marks_local_read_when_provider_fails():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_unread_email(db)
    email_id = email.id
    db.close()

    client = _client(SessionLocal)
    mark_read = AsyncMock(side_effect=RuntimeError("quota"))
    ensure_token = AsyncMock(return_value="token")

    with (
        patch("app.api.emails.gmail_mark_read", mark_read),
        patch("app.api.emails.ensure_google_access_token", ensure_token),
        patch("app.api.emails.SessionLocal", SessionLocal),
    ):
        res = client.get(f"/api/emails/{email_id}")
        assert res.status_code == 200
        assert res.json()["is_read"] is True
        mark_read.assert_awaited_once()
