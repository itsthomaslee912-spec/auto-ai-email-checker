from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import emails
from app.db import Base, get_db
from app.models import EmailLabel, EmailMessage, MailboxConnection, MailFolder, Provider, User


def _client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    user = User(external_id="u-search")
    db.add(user)
    db.flush()
    first = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address="one@example.com",
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    second = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address="two@example.com",
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    db.add_all([first, second])
    db.flush()
    db.add_all(
        [
            EmailMessage(
                mailbox_id=first.id,
                provider_message_id="a-interview",
                subject="Acme interview",
                sender="hr@acme.com",
                snippet="schedule",
                label=EmailLabel.INTERVIEW_SCHEDULED.value,
                interview_subtype="confirmation",
                folder=MailFolder.INBOX.value,
                company="Acme",
                job_role="Engineer",
            ),
            EmailMessage(
                mailbox_id=first.id,
                provider_message_id="a-reminder-1",
                subject="Beta interview reminder",
                sender="hr@beta.com",
                snippet="tomorrow",
                label=EmailLabel.INTERVIEW_SCHEDULED.value,
                interview_subtype="reminder",
                folder=MailFolder.INBOX.value,
            ),
            EmailMessage(
                mailbox_id=first.id,
                provider_message_id="a-reminder-2",
                subject="Gamma interview reminder",
                sender="hr@gamma.com",
                snippet="next week",
                label=EmailLabel.INTERVIEW_SCHEDULED.value,
                interview_subtype="reminder",
                folder=MailFolder.INBOX.value,
            ),
            EmailMessage(
                mailbox_id=second.id,
                provider_message_id="b-offer",
                subject="Acme offer follow up",
                sender="jobs@other.com",
                snippet="next steps",
                label=EmailLabel.APPLICATION_CONFIRMATION.value,
                folder=MailFolder.INBOX.value,
            ),
            EmailMessage(
                mailbox_id=first.id,
                provider_message_id="a-sender",
                subject="Hello",
                sender="pat@acme.com",
                snippet="unrelated note",
                label=EmailLabel.OTHER.value,
                folder=MailFolder.SPAM.value,
            ),
            EmailMessage(
                mailbox_id=first.id,
                provider_message_id="a-trash",
                subject="Acme screening",
                sender="talent@acme.com",
                snippet="thanks",
                label=EmailLabel.SCREENING.value,
                folder=MailFolder.TRASH.value,
                company="Acme",
                job_role="Designer",
            ),
        ]
    )
    db.commit()
    first_id = first.id
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
    return TestClient(app), first_id


def test_search_covers_every_account_when_mailbox_is_omitted():
    client, first_id = _client()
    found = client.get("/api/emails", params={"q": "Acme interview"})
    assert found.status_code == 200
    subjects = {item["subject"] for item in found.json()["items"]}
    assert subjects == {"Acme interview"}

    both = client.get("/api/emails", params={"q": "acme"})
    assert both.status_code == 200
    body = both.json()
    mailboxes = {item["mailbox_id"] for item in body["items"]}
    assert first_id in mailboxes
    assert len(mailboxes) == 2
    assert body["folder_counts"]["spam"] == 1
    assert body["folder_counts"]["trash"] == 1

    scoped = client.get("/api/emails", params={"q": "acme", "mailbox_id": first_id})
    assert {item["mailbox_id"] for item in scoped.json()["items"]} == {first_id}


def test_search_matches_sender_and_rejects_unrelated_query():
    client, _first_id = _client()
    sender = client.get("/api/emails", params={"q": "pat@acme.com"})
    assert sender.status_code == 200
    assert [item["subject"] for item in sender.json()["items"]] == ["Hello"]

    empty = client.get("/api/emails", params={"q": "zzzz-not-a-mail"})
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0


def test_search_keeps_folder_and_label_filters():
    client, _first_id = _client()
    inbox = client.get("/api/emails", params={"q": "acme", "folder": "inbox"})
    assert inbox.status_code == 200
    assert {item["folder"] for item in inbox.json()["items"]} == {"inbox"}
    assert inbox.json()["folder_counts"]["trash"] == 1

    interview = client.get("/api/emails", params={"q": "acme", "label": "interview_scheduled"})
    assert interview.status_code == 200
    assert [item["label"] for item in interview.json()["items"]] == ["interview_scheduled"]

    role = client.get("/api/emails", params={"q": "Designer"})
    assert [item["subject"] for item in role.json()["items"]] == ["Acme screening"]


def test_interview_subtype_filter_applies_before_pagination():
    client, first_id = _client()
    first = client.get("/api/emails", params={
        "label": "interview_scheduled", "interview_subtype": "reminder",
        "mailbox_id": first_id, "limit": 1,
    })
    assert first.status_code == 200
    first_page = first.json()
    assert first_page["total"] == 2
    assert first_page["has_more"] is True
    assert first_page["items"][0]["interview_subtype"] == "reminder"

    second = client.get("/api/emails", params={
        "label": "interview_scheduled", "interview_subtype": "reminder",
        "mailbox_id": first_id, "limit": 1,
        "before_id": first_page["next_cursor"],
    })
    assert second.status_code == 200
    second_page = second.json()
    assert second_page["total"] == 2
    assert second_page["has_more"] is False
    assert second_page["items"][0]["id"] != first_page["items"][0]["id"]
    assert second_page["items"][0]["interview_subtype"] == "reminder"

    confirmation = client.get("/api/emails", params={"interview_subtype": "confirmation"})
    assert confirmation.json()["total"] == 1
    assert confirmation.json()["items"][0]["label"] == "interview_scheduled"

    invalid = client.get("/api/emails", params={"interview_subtype": "unknown"})
    assert invalid.status_code == 422
