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

        async def publish(_event, _payload):
            return None

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
    engine.dispose()
