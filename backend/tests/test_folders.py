from __future__ import annotations

from datetime import datetime, timezone

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
from app.email.gmail import GMAIL_COMBINED_QUERY, _gmail_mailbox_query, normalize_gmail_message
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
    assert folder_from_outlook_well_known("sentitems") == MailFolder.SENT.value
    assert folder_from_outlook_well_known("Sent Items") == MailFolder.SENT.value
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
        "payload": {"headers": [
            {"name": "Subject", "value": "Hi"},
            {"name": "To", "value": "Deana Coffey <deana.coffey@example.com>"},
        ]},
    }
    assert normalize_gmail_message(raw)["folder"] == MailFolder.SPAM.value
    assert normalize_gmail_message(raw)["recipients"] == "Deana Coffey <deana.coffey@example.com>"
    sent = {
        "id": "g-sent",
        "labelIds": ["SENT"],
        "snippet": "available Tuesday",
        "payload": {"headers": [{"name": "Subject", "value": "Re: Interview"}]},
    }
    assert normalize_gmail_message(sent)["folder"] == MailFolder.SENT.value


def test_gmail_mailbox_query_keeps_sent_from_account():
    assert "in:sent" in GMAIL_COMBINED_QUERY
    query = _gmail_mailbox_query(GMAIL_COMBINED_QUERY, "jordan@example.com")
    assert "in:sent from:jordan@example.com" in query
    assert "to:jordan@example.com" in query
    inbox_only = _gmail_mailbox_query("in:inbox", "jordan@example.com")
    assert "in:sent from:" not in inbox_only


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
        "toRecipients": [{"emailAddress": {"name": "Deana Coffey", "address": "deana@example.com"}}],
        "body": {"contentType": "text", "content": "hello"},
    }
    assert normalize_outlook_message(raw)["folder"] == MailFolder.TRASH.value
    assert normalize_outlook_message(raw)["recipients"] == "Deana Coffey <deana@example.com>"


def test_normalize_outlook_message_keeps_unread_state():
    raw = {
        "id": "o-unread",
        "isRead": False,
        "from": {"emailAddress": {"address": "a@b.com"}},
        "body": {"contentType": "text", "content": "hello"},
    }
    assert normalize_outlook_message(raw)["is_read"] is False


def test_normalize_outlook_meeting_request_preserves_structured_calendar_time():
    raw = {
        "@odata.type": "#microsoft.graph.eventMessageRequest",
        "id": "o-meeting",
        "subject": "Platform Engineer - Thomas Lee",
        "meetingMessageType": "meetingRequest",
        "startDateTime": {"dateTime": "2026-10-27T22:00:00.0000000", "timeZone": "UTC"},
        "endDateTime": {"dateTime": "2026-10-27T22:30:00.0000000", "timeZone": "UTC"},
        "location": {"displayName": "Microsoft Teams Meeting"},
        "from": {"emailAddress": {"address": "recruiter@msci.com"}},
        "body": {"contentType": "text", "content": "Join the Teams meeting."},
    }

    normalized = normalize_outlook_message(raw)

    assert "METHOD:REQUEST" in normalized["body_text"]
    assert "DTSTART:20261027T220000Z" in normalized["body_text"]
    assert "DTEND:20261027T223000Z" in normalized["body_text"]
    assert "SUMMARY:Platform Engineer - Thomas Lee" in normalized["body_text"]


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
    # Sidebar unread badges count every folder, matching the all-mail list.
    assert body["mailbox_unread_counts"] == {str(mailbox_id): 4}

    mixed = client.get("/api/emails")
    assert mixed.status_code == 200
    assert mixed.json()["total"] == 4
    assert mixed.json()["mailbox_unread_counts"] == {str(mailbox_id): 4}


def test_pending_classification_is_hidden_from_lists_counts_details_and_conversations():
    SessionLocal = _session_factory()
    with SessionLocal() as db:
        user = User(external_id="u-pending-hidden")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.GOOGLE.value,
            email_address="pending@example.com",
            access_token_enc="x",
            refresh_token_enc="",
            is_active=True,
        )
        db.add(mailbox)
        db.flush()
        mailbox_id = mailbox.id
        visible = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="classified",
            thread_id="same-thread",
            subject="Classified interview",
            sender="recruiter@example.com",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            received_at=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
            classification_pending=False,
        )
        pending = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="pending",
            thread_id="same-thread",
            subject="Temporary Other",
            sender="recruiter@example.com",
            label=EmailLabel.OTHER.value,
            received_at=datetime(2026, 10, 20, 13, tzinfo=timezone.utc),
            classification_pending=True,
        )
        db.add_all([visible, pending])
        db.commit()
        visible_id, pending_id = visible.id, pending.id

    app = FastAPI()
    app.include_router(emails.router)

    def override_get_db():
        with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    flat = client.get("/api/emails").json()
    assert flat["total"] == 1
    assert [item["id"] for item in flat["items"]] == [visible_id]
    assert flat["label_counts"][EmailLabel.OTHER.value] == 0
    assert flat["mailbox_unread_counts"] == {str(mailbox_id): 1}

    grouped = client.get("/api/emails", params={"conversation": "true"}).json()
    assert grouped["total"] == 1
    assert grouped["items"][0]["id"] == visible_id
    assert grouped["items"][0]["thread_count"] == 1

    assert client.get(f"/api/emails/{pending_id}").status_code == 404
    conversation = client.get(f"/api/emails/{visible_id}/conversation").json()
    assert [item["id"] for item in conversation] == [visible_id]


def test_conversation_view_keeps_the_newest_message_in_each_thread():
    SessionLocal = _session_factory()
    db = SessionLocal()
    user = User(external_id="u-thread")
    db.add(user)
    db.flush()
    mailbox = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address="thread@example.com",
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    db.add(mailbox)
    db.flush()
    older = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    newer = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    db.add_all([
        EmailMessage(
            mailbox_id=mailbox.id,
            provider_message_id="thread-old",
            thread_id="same-thread",
            subject="Older note",
            sender="a@b.com",
            snippet="old",
            label=EmailLabel.OTHER.value,
            folder=MailFolder.INBOX.value,
            received_at=older,
            is_read=True,
        ),
        EmailMessage(
            mailbox_id=mailbox.id,
            provider_message_id="thread-new",
            thread_id="same-thread",
            subject="Newer note",
            sender="a@b.com",
            snippet="new",
            label=EmailLabel.OTHER.value,
            folder=MailFolder.INBOX.value,
            received_at=newer,
            is_read=True,
        ),
        EmailMessage(
            mailbox_id=mailbox.id,
            provider_message_id="alone",
            thread_id=None,
            subject="Solo",
            sender="a@b.com",
            snippet="solo",
            label=EmailLabel.OTHER.value,
            folder=MailFolder.INBOX.value,
            received_at=older,
            is_read=True,
        ),
    ])
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

    grouped = client.get("/api/emails", params={"conversation": "true"})
    assert grouped.status_code == 200
    body = grouped.json()
    assert body["total"] == 2
    subjects = [item["subject"] for item in body["items"]]
    assert subjects == ["Newer note", "Solo"]
    assert body["items"][0]["thread_count"] == 2
    assert body["items"][1]["thread_count"] == 1

    flat = client.get("/api/emails")
    assert flat.json()["total"] == 3


def test_conversation_view_uses_unread_state_from_the_whole_thread():
    SessionLocal = _session_factory()
    db = SessionLocal()
    user = User(external_id="u-thread-unread")
    db.add(user)
    db.flush()
    mailbox = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address="thread-unread@example.com",
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    db.add(mailbox)
    db.flush()
    older = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    newer = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    db.add_all([
        EmailMessage(
            mailbox_id=mailbox.id,
            provider_message_id="unread-old",
            thread_id="mixed-read-thread",
            subject="Older unread",
            sender="a@b.com",
            snippet="old",
            label=EmailLabel.OTHER.value,
            received_at=older,
            is_read=False,
        ),
        EmailMessage(
            mailbox_id=mailbox.id,
            provider_message_id="read-new",
            thread_id="mixed-read-thread",
            subject="Newer read",
            sender="a@b.com",
            snippet="new",
            label=EmailLabel.OTHER.value,
            received_at=newer,
            is_read=True,
        ),
    ])
    mailbox_id = mailbox.id
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
    body = TestClient(app).get(
        "/api/emails",
        params={"conversation": "true", "inbox_type": "unread_first"},
    ).json()

    assert body["total"] == 1
    assert body["items"][0]["subject"] == "Newer read"
    assert body["items"][0]["is_read"] is False
    assert body["mailbox_unread_counts"] == {str(mailbox_id): 1}
