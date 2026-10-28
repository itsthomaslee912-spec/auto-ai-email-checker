from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import EmailLabel, EmailMessage, MailboxConnection, Provider, User
from app.schemas import ClassificationResult
from app.services.ingest import ingest_normalized_message


def test_concurrent_duplicate_import_does_not_fail_sync(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'sync.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as db:
        user = User(external_id="duplicate-sync")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.GOOGLE.value,
            email_address="test@example.com",
            access_token_enc="x",
            refresh_token_enc="",
            is_active=True,
        )
        db.add(mailbox)
        db.commit()
        mailbox_id = mailbox.id

    async def classify_after_other_import(**kwargs):
        with Session() as other:
            other.add(EmailMessage(
                mailbox_id=mailbox_id,
                provider_message_id="same-message",
                subject="Already imported",
                sender="sender@example.com",
                snippet="",
                label=EmailLabel.OTHER.value,
            ))
            other.commit()
        return ClassificationResult(label=EmailLabel.OTHER.value)

    monkeypatch.setattr("app.services.ingest.classify_email", classify_after_other_import)
    with Session() as db:
        mailbox = db.get(MailboxConnection, mailbox_id)
        result = asyncio.run(ingest_normalized_message(
            db, mailbox,
            {"provider_message_id": "same-message", "subject": "Another import", "sender": "sender@example.com"},
        ))
        assert result is None
        assert db.query(EmailMessage).count() == 1


def test_download_only_queues_other_without_ai_processing(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'download.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as db:
        user = User(external_id="download-only")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id, provider=Provider.GOOGLE.value,
            email_address="download@example.com", access_token_enc="x", is_active=True,
        )
        db.add(mailbox)
        db.commit()

        async def unexpected(**_kwargs):
            raise AssertionError("AI processing must not run during download-only sync")

        published = []

        async def publish(event, payload):
            published.append((event, payload))

        monkeypatch.setattr("app.services.ingest.classify_email", unexpected)
        monkeypatch.setattr("app.services.ingest.apply_outcome", unexpected)
        monkeypatch.setattr("app.services.ingest.publish", publish)

        email = asyncio.run(ingest_normalized_message(
            db, mailbox,
            {"provider_message_id": "new-message", "subject": "Interview", "sender": "test@example.com"},
            download_only=True,
        ))
        assert email is not None
        assert email.label == EmailLabel.OTHER.value
        assert email.classification_pending is True
        assert email.confidence is None
        assert email.interview_subtype is None
        assert email.outcome_extracted is False
        assert published == []
    engine.dispose()


def test_download_only_existing_refresh_defers_calendar_and_classification(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'refresh.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as db:
        user = User(external_id="refresh-existing")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(user_id=user.id, provider=Provider.GOOGLE.value, email_address="refresh@example.com", access_token_enc="x", is_active=True)
        db.add(mailbox)
        db.flush()
        email = EmailMessage(mailbox_id=mailbox.id, provider_message_id="calendar-message", thread_id="thread-old", subject="Interview", sender="recruiter@example.com", body_text="Confirmation only", label=EmailLabel.INTERVIEW_SCHEDULED.value)
        db.add(email)
        db.commit()

        async def process(*_args, **_kwargs):
            raise AssertionError("download-only sync must not run calendar reconciliation")

        monkeypatch.setattr("app.services.ingest.process_calendar_email", process)
        result = asyncio.run(ingest_normalized_message(db, mailbox, {
            "provider_message_id": "calendar-message",
            "thread_id": "thread-new",
            "subject": "Interview",
            "sender": "recruiter@example.com",
            "body_text": "Confirmation only\nBEGIN:VCALENDAR\nDTSTART:20261005T140000Z",
        }, download_only=True))

        assert result is None
        db.refresh(email)
        assert email.thread_id == "thread-new"
        assert "BEGIN:VCALENDAR" in email.body_text
        assert email.classification_pending is True
        assert email.calendar_processed_hash is None
    engine.dispose()


def test_existing_message_sync_never_resets_local_read_state(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'read-state.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as db:
        user = User(external_id="read-state-sync")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.MICROSOFT.value,
            email_address="read@example.com",
            access_token_enc="x",
            is_active=True,
        )
        db.add(mailbox)
        db.flush()
        email = EmailMessage(
            mailbox_id=mailbox.id,
            provider_message_id="already-read",
            subject="Interview",
            sender="recruiter@example.com",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            is_read=True,
        )
        db.add(email)
        db.commit()

        monkeypatch.setattr("app.services.ingest.process_calendar_email", lambda *_args, **_kwargs: None)
        asyncio.run(ingest_normalized_message(db, mailbox, {
            "provider_message_id": "already-read",
            "subject": "Interview",
            "sender": "recruiter@example.com",
            "is_read": False,
        }, download_only=True))

        db.refresh(email)
        assert email.is_read is True
    engine.dispose()


def test_existing_message_sync_can_promote_unread_to_read(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'provider-read.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as db:
        user = User(external_id="provider-read-sync")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.GOOGLE.value,
            email_address="provider-read@example.com",
            access_token_enc="x",
            is_active=True,
        )
        db.add(mailbox)
        db.flush()
        email = EmailMessage(
            mailbox_id=mailbox.id,
            provider_message_id="provider-read",
            subject="Update",
            sender="sender@example.com",
            label=EmailLabel.OTHER.value,
            is_read=False,
        )
        db.add(email)
        db.commit()

        asyncio.run(ingest_normalized_message(db, mailbox, {
            "provider_message_id": "provider-read",
            "subject": "Update",
            "sender": "sender@example.com",
            "is_read": True,
        }, download_only=True))

        db.refresh(email)
        assert email.is_read is True
    engine.dispose()


def test_existing_message_keeps_provider_refresh_when_calendar_repair_fails(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'failed-repair.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as db:
        user = User(external_id="failed-calendar-repair")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(user_id=user.id, provider=Provider.GOOGLE.value, email_address="repair@example.com", access_token_enc="x", is_active=True)
        db.add(mailbox)
        db.flush()
        email = EmailMessage(mailbox_id=mailbox.id, provider_message_id="calendar-message", subject="Interview", sender="recruiter@example.com", body_text="Incomplete", label=EmailLabel.INTERVIEW_SCHEDULED.value)
        db.add(email)
        db.commit()

        async def fail_repair(*_args, **_kwargs):
            raise RuntimeError("legacy repair failed")

        monkeypatch.setattr("app.services.ingest.process_calendar_email", fail_repair)
        async def classify(**_kwargs):
            return ClassificationResult(label=EmailLabel.INTERVIEW_SCHEDULED.value)

        monkeypatch.setattr("app.services.ingest.classify_email", classify)
        result = asyncio.run(ingest_normalized_message(db, mailbox, {
            "provider_message_id": "calendar-message",
            "subject": "Interview",
            "sender": "recruiter@example.com",
            "body_text": "Complete provider content",
        }))

        assert result is None
        db.refresh(email)
        assert email.body_text == "Complete provider content"
    engine.dispose()
