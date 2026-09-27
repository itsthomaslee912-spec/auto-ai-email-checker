from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import emails, mailboxes
from app.db import Base, get_db
from app.models import (
    ClassifyCorrection,
    EmailLabel,
    EmailMessage,
    MailboxConnection,
    Provider,
    User,
)
from app.services.mailbox_cleanup import delete_emails_for_mailbox, purge_orphaned_emails


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
    app.include_router(mailboxes.router)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _seed_mailbox(db, *, external_id: str, email_address: str, provider_message_id: str) -> tuple[MailboxConnection, EmailMessage]:
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
    email = EmailMessage(
        mailbox_id=mailbox.id,
        provider_message_id=provider_message_id,
        subject=f"Hello {email_address}",
        sender="recruiter@example.com",
        snippet="interview next week",
        body_text="Can you interview next week?",
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
    )
    db.add(email)
    db.commit()
    db.refresh(mailbox)
    db.refresh(email)
    return mailbox, email


def test_disconnect_deletes_emails_and_corrections():
    SessionLocal = _session_factory()
    db = SessionLocal()
    thomas, thomas_email = _seed_mailbox(
        db,
        external_id="u1",
        email_address="itsthomaslee912@gmail.com",
        provider_message_id="thomas-1",
    )
    jordan, jordan_email = _seed_mailbox(
        db,
        external_id="u1",
        email_address="jordanwm914@gmail.com",
        provider_message_id="jordan-1",
    )
    db.add(
        ClassifyCorrection(
            email_id=thomas_email.id,
            previous_label=EmailLabel.OTHER.value,
            corrected_label=EmailLabel.INTERVIEW_SCHEDULED.value,
            subject=thomas_email.subject,
            sender=thomas_email.sender,
            snippet=thomas_email.snippet,
            body_text=thomas_email.body_text,
        )
    )
    db.commit()
    thomas_id = thomas.id
    jordan_email_id = jordan_email.id
    db.close()

    client = _client(SessionLocal)
    listed = client.get("/api/emails")
    assert listed.status_code == 200
    assert listed.json()["total"] == 2

    res = client.delete(f"/api/mailboxes/{thomas_id}")
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert res.json()["deleted_emails"] == 1

    remaining = client.get("/api/emails")
    assert remaining.status_code == 200
    body = remaining.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == jordan_email_id
    assert str(thomas_id) not in body["mailbox_counts"]

    boxes = client.get("/api/mailboxes")
    assert boxes.status_code == 200
    emails_shown = {row["email_address"] for row in boxes.json()}
    assert emails_shown == {"jordanwm914@gmail.com"}

    db = SessionLocal()
    assert db.query(EmailMessage).filter(EmailMessage.mailbox_id == thomas_id).count() == 0
    assert db.query(ClassifyCorrection).count() == 0
    stored = db.query(MailboxConnection).filter(MailboxConnection.id == thomas_id).one()
    assert stored.is_active is False
    assert db.query(EmailMessage).filter(EmailMessage.id == jordan_email_id).count() == 1
    db.close()


def test_list_emails_hides_disconnected_mailbox_mail():
    SessionLocal = _session_factory()
    db = SessionLocal()
    thomas, _thomas_email = _seed_mailbox(
        db,
        external_id="u1",
        email_address="itsthomaslee912@gmail.com",
        provider_message_id="thomas-1",
    )
    jordan, jordan_email = _seed_mailbox(
        db,
        external_id="u1",
        email_address="jordanwm914@gmail.com",
        provider_message_id="jordan-1",
    )
    thomas.is_active = False
    db.commit()
    thomas_id = thomas.id
    jordan_email_id = jordan_email.id
    db.close()

    client = _client(SessionLocal)
    body = client.get("/api/emails").json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == jordan_email_id
    assert str(thomas_id) not in body["mailbox_counts"]


def test_purge_orphaned_emails_removes_disconnected_and_missing_mailbox_rows():
    SessionLocal = _session_factory()
    db = SessionLocal()
    disconnected, disconnected_email = _seed_mailbox(
        db,
        external_id="u1",
        email_address="itsthomaslee912@gmail.com",
        provider_message_id="thomas-1",
    )
    missing, missing_email = _seed_mailbox(
        db,
        external_id="u1",
        email_address="gone@example.com",
        provider_message_id="gone-1",
    )
    _keep, keep_email = _seed_mailbox(
        db,
        external_id="u1",
        email_address="jordanwm914@gmail.com",
        provider_message_id="jordan-1",
    )
    db.add(
        ClassifyCorrection(
            email_id=disconnected_email.id,
            previous_label=EmailLabel.OTHER.value,
            corrected_label=EmailLabel.INTERVIEW_SCHEDULED.value,
        )
    )
    disconnected.is_active = False
    db.commit()
    missing_id = missing.id
    keep_email_id = keep_email.id
    db.execute(text("DELETE FROM mailbox_connections WHERE id = :id"), {"id": missing_id})
    db.commit()

    deleted = purge_orphaned_emails(db)
    db.commit()
    assert deleted == 2
    assert db.query(EmailMessage).count() == 1
    assert db.query(EmailMessage).one().id == keep_email_id
    assert db.query(ClassifyCorrection).count() == 0
    db.close()


def test_delete_emails_for_mailbox_is_scoped():
    SessionLocal = _session_factory()
    db = SessionLocal()
    thomas, _ = _seed_mailbox(
        db,
        external_id="u1",
        email_address="itsthomaslee912@gmail.com",
        provider_message_id="thomas-1",
    )
    jordan, jordan_email = _seed_mailbox(
        db,
        external_id="u1",
        email_address="jordanwm914@gmail.com",
        provider_message_id="jordan-1",
    )
    deleted = delete_emails_for_mailbox(db, thomas.id)
    db.commit()
    assert deleted == 1
    assert db.query(EmailMessage).count() == 1
    assert db.query(EmailMessage).one().id == jordan_email.id
    db.close()
