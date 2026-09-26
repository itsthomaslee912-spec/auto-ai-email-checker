from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import emails
from app.db import Base, get_db
from app.email.folders import (
    folder_from_gmail_labels,
    folder_from_outlook_well_known,
    normalize_folder,
)
from app.email.gmail import normalize_gmail_message
from app.email.outlook import normalize_outlook_message
from app.models import EmailLabel, EmailMessage, MailboxConnection, MailFolder, Provider, User


def test_gmail_label_priority():
    assert folder_from_gmail_labels(["INBOX", "UNREAD"]) == MailFolder.INBOX.value
    assert folder_from_gmail_labels(["INBOX", "SPAM"]) == MailFolder.SPAM.value
    assert folder_from_gmail_labels(["INBOX", "TRASH"]) == MailFolder.TRASH.value
    assert folder_from_gmail_labels(["SPAM", "TRASH"]) == MailFolder.TRASH.value
    assert folder_from_gmail_labels(["CATEGORY_UPDATES"]) == MailFolder.ARCHIVE.value
    assert folder_from_gmail_labels(None) == MailFolder.ARCHIVE.value


def test_outlook_well_known_names():
    assert folder_from_outlook_well_known("inbox") == MailFolder.INBOX.value
    assert folder_from_outlook_well_known("junkemail") == MailFolder.SPAM.value
    assert folder_from_outlook_well_known("Junk Email") == MailFolder.SPAM.value
    assert folder_from_outlook_well_known("deleteditems") == MailFolder.TRASH.value
    assert folder_from_outlook_well_known("Deleted Items") == MailFolder.TRASH.value
    assert folder_from_outlook_well_known("archive") == MailFolder.ARCHIVE.value
    assert folder_from_outlook_well_known("unknown") == MailFolder.INBOX.value


def test_normalize_folder_defaults_inbox():
    assert normalize_folder("spam") == MailFolder.SPAM.value
    assert normalize_folder("TRASH") == MailFolder.TRASH.value
    assert normalize_folder("nope") == MailFolder.INBOX.value
    assert normalize_folder(None) == MailFolder.INBOX.value


def test_normalize_gmail_message_stamps_folder():
    raw = {
        "id": "g1",
        "labelIds": ["SPAM"],
        "snippet": "hello",
        "payload": {"headers": [{"name": "Subject", "value": "Hi"}]},
    }
    assert normalize_gmail_message(raw)["folder"] == MailFolder.SPAM.value


def test_normalize_gmail_message_keeps_unread_state():
    raw = {
        "id": "g-unread",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {"headers": []},
    }
    assert normalize_gmail_message(raw)["is_read"] is False


def test_normalize_outlook_message_stamps_folder():
    raw = {
        "id": "o1",
        "subject": "Hi",
        "_mail_folder": "deleteditems",
        "from": {"emailAddress": {"address": "a@b.com"}},
        "body": {"contentType": "text", "content": "hello"},
    }
    assert normalize_outlook_message(raw)["folder"] == MailFolder.TRASH.value


def test_normalize_outlook_message_keeps_unread_state():
    raw = {
        "id": "o-unread",
        "isRead": False,
        "from": {"emailAddress": {"address": "a@b.com"}},
        "body": {"contentType": "text", "content": "hello"},
    }
    assert normalize_outlook_message(raw)["is_read"] is False


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def test_list_emails_filters_folder():
    SessionLocal = _session_factory()
    db = SessionLocal()
    user = User(external_id="u-folder")
    db.add(user)
    db.flush()
    mailbox = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address="folder@example.com",
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    db.add(mailbox)
    db.flush()
    mailbox_id = mailbox.id
    db.add_all(
        [
            EmailMessage(
                mailbox_id=mailbox.id,
                provider_message_id="in1",
                subject="Inbox",
                sender="a@b.com",
                snippet="hi",
                label=EmailLabel.OTHER.value,
                folder=MailFolder.INBOX.value,
            ),
            EmailMessage(
                mailbox_id=mailbox.id,
                provider_message_id="sp1",
                subject="Spam",
                sender="a@b.com",
                snippet="hi",
                label=EmailLabel.OTHER.value,
                folder=MailFolder.SPAM.value,
            ),
            EmailMessage(
                mailbox_id=mailbox.id,
                provider_message_id="tr1",
                subject="Trash",
                sender="a@b.com",
                snippet="hi",
                label=EmailLabel.OTHER.value,
                folder=MailFolder.TRASH.value,
            ),
            EmailMessage(
                mailbox_id=mailbox.id,
                provider_message_id="sent1",
                subject="Sent",
                sender="a@b.com",
                snippet="hi",
                label=EmailLabel.OTHER.value,
                folder=MailFolder.SENT.value,
            ),
        ]
    )
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(emails.router)

    def override_get_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    spam = client.get("/api/emails", params={"folder": "spam"})
    assert spam.status_code == 200
    body = spam.json()
    assert body["total"] == 1
    assert body["items"][0]["folder"] == "spam"
    assert body["folder_counts"]["inbox"] == 1
    assert body["folder_counts"]["spam"] == 1
    assert body["folder_counts"]["trash"] == 1
    # Sidebar unread badges include only unread messages currently in Inbox.
    assert body["mailbox_unread_counts"] == {str(mailbox_id): 1}

    mixed = client.get("/api/emails")
    assert mixed.status_code == 200
    assert mixed.json()["total"] == 4
    assert mixed.json()["mailbox_unread_counts"] == {str(mailbox_id): 1}
