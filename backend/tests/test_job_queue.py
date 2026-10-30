from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import MailboxConnection, MailboxJobState, Provider, User
from app.services import job_queue


def test_mailbox_job_is_durable_and_claimed_once(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(job_queue, "SessionLocal", session_factory)

    with session_factory() as db:
        user = User(external_id="job-queue-test")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.MICROSOFT.value,
            email_address="queue@example.com",
            access_token_enc="x",
            is_active=True,
        )
        db.add(mailbox)
        db.commit()
        mailbox_id = mailbox.id

        queued = job_queue.enqueue(
            db, mailbox_id, "sync", options={"force_full": True}
        )

    assert queued["state"] == "queued"
    assert job_queue.claim("sync") == [(mailbox_id, {"force_full": True})]
    assert job_queue.claim("sync") == []
    with session_factory() as db:
        claimed = db.query(MailboxJobState).one()
        assert claimed.claimed_by == job_queue.INSTANCE_ID
        assert claimed.lease_expires_at is not None
        assert claimed.attempts == 1

    job_queue.persist(mailbox_id, "sync", {
        "mailbox_id": mailbox_id,
        "state": "done",
        "imported": 4,
        "message": "Synced 4 new messages",
    })
    with session_factory() as db:
        status = job_queue.get_status(db, mailbox_id, "sync")
    assert status["state"] == "done"
    assert status["imported"] == 4
    with session_factory() as db:
        finished = db.query(MailboxJobState).one()
        assert finished.claimed_by is None
        assert finished.lease_expires_at is None


def test_expired_worker_lease_returns_job_to_queue(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    session_factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(job_queue, "SessionLocal", session_factory)

    with session_factory() as db:
        user = User(external_id="expired-job-test")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.GOOGLE.value,
            email_address="expired@example.com",
            access_token_enc="x",
            is_active=True,
        )
        db.add(mailbox)
        db.flush()
        db.add(MailboxJobState(
            mailbox_id=mailbox.id,
            job_type="sync",
            state="running",
            options='{"force_full":false}',
            progress='{"state":"running"}',
            claimed_by="dead-replica",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            attempts=1,
        ))
        db.commit()
        mailbox_id = mailbox.id

    assert job_queue.claim("sync") == [(mailbox_id, {"force_full": False})]
    with session_factory() as db:
        recovered = db.query(MailboxJobState).one()
        assert recovered.state == "starting"
        assert recovered.claimed_by == job_queue.INSTANCE_ID
        assert recovered.attempts == 2


def test_pending_reclassify_mode_survives_lease_recovery(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    session_factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(job_queue, "SessionLocal", session_factory)

    with session_factory() as db:
        user = User(external_id="pending-reclassify-mode")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.GOOGLE.value,
            email_address="pending@example.com",
            access_token_enc="x",
            is_active=True,
        )
        db.add(mailbox)
        db.commit()
        mailbox_id = mailbox.id

    job_queue.persist(mailbox_id, "reclassify", {
        "mailbox_id": mailbox_id,
        "state": "running",
        "pending_only": True,
        "processed": 0,
        "total": 2,
        "message": "Classifying queued emails",
    })
    with session_factory() as db:
        row = db.query(MailboxJobState).one()
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    assert job_queue.claim("reclassify") == [(mailbox_id, {"pending_only": True})]
