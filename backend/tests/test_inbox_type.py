from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
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


def _seed(db) -> MailboxConnection:
    user = User(external_id="u-inbox-type")
    db.add(user)
    db.flush()
    mailbox = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address="inbox-type@example.com",
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    db.add(mailbox)
    db.flush()

    rows = [
        # older unread
        ("u-old", False, datetime(2026, 9, 20, 10, 0, 0)),
        # newer unread
        ("u-new", False, datetime(2026, 9, 21, 12, 0, 0)),
        # older read (should still come after all unread)
        ("r-old", True, datetime(2026, 9, 19, 9, 0, 0)),
        # newest overall but read
        ("r-new", True, datetime(2026, 9, 21, 18, 0, 0)),
    ]
    for mid, is_read, received_at in rows:
        db.add(
            EmailMessage(
                mailbox_id=mailbox.id,
                provider_message_id=mid,
                subject=mid,
                sender="s@example.com",
                snippet=mid,
                body_text=mid,
                label=EmailLabel.OTHER.value,
                is_read=is_read,
                received_at=received_at,
            )
        )
    db.commit()
    db.refresh(mailbox)
    return mailbox


def test_unread_first_orders_unread_before_read():
    SessionLocal = _session_factory()
    db = SessionLocal()
    _seed(db)
    db.close()

    client = _client(SessionLocal)
    res = client.get("/api/emails", params={"inbox_type": "unread_first", "limit": 10})
    assert res.status_code == 200
    body = res.json()
    subjects = [item["subject"] for item in body["items"]]
    assert subjects == ["u-new", "u-old", "r-new", "r-old"]
    assert body["next_is_read"] is True


def test_unread_first_cursor_stays_in_bucket():
    SessionLocal = _session_factory()
    db = SessionLocal()
    _seed(db)
    db.close()

    client = _client(SessionLocal)
    first = client.get("/api/emails", params={"inbox_type": "unread_first", "limit": 2})
    assert first.status_code == 200
    page = first.json()
    assert [item["subject"] for item in page["items"]] == ["u-new", "u-old"]
    assert page["has_more"] is True
    assert page["next_is_read"] is False

    second = client.get(
        "/api/emails",
        params={
            "inbox_type": "unread_first",
            "limit": 2,
            "before_id": page["next_cursor"],
            "before_received_at": page["next_received_at"],
            "before_is_read": page["next_is_read"],
        },
    )
    assert second.status_code == 200
    page2 = second.json()
    assert [item["subject"] for item in page2["items"]] == ["r-new", "r-old"]
    assert page2["has_more"] is False


def test_default_inbox_type_is_chronological():
    SessionLocal = _session_factory()
    db = SessionLocal()
    _seed(db)
    db.close()

    client = _client(SessionLocal)
    res = client.get("/api/emails", params={"limit": 10})
    assert res.status_code == 200
    subjects = [item["subject"] for item in res.json()["items"]]
    assert subjects == ["r-new", "u-new", "u-old", "r-old"]
