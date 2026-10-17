import asyncio
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import WebhookDeliveryJob
from app.services import webhook_queue


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def test_enqueue_is_committed_before_ack_and_deduplicates(monkeypatch):
    factory = _session_factory()
    monkeypatch.setattr(webhook_queue, "SessionLocal", factory)
    payload = {"email_address": "person@example.com", "history_id": "42"}

    assert webhook_queue.enqueue_webhook_delivery(
        "google", payload, provider_id="provider-message-1"
    )
    assert not webhook_queue.enqueue_webhook_delivery(
        "google", payload, provider_id="provider-message-1"
    )

    with factory() as db:
        jobs = db.query(WebhookDeliveryJob).all()
        assert len(jobs) == 1
        assert jobs[0].status == "pending"
        assert json.loads(jobs[0].payload) == payload


def test_interrupted_job_is_recovered_and_completed(monkeypatch):
    factory = _session_factory()
    monkeypatch.setattr(webhook_queue, "SessionLocal", factory)
    seen = {}

    async def process(_db, email_address, history_id):
        seen.update(email_address=email_address, history_id=history_id)
        return 1

    monkeypatch.setattr(webhook_queue, "process_gmail_notification", process)
    webhook_queue.enqueue_webhook_delivery(
        "google",
        {"email_address": "person@example.com", "history_id": "99"},
        provider_id="provider-message-2",
    )

    first_claim = webhook_queue._claim_next_job()
    assert first_claim is not None
    webhook_queue._recover_interrupted_jobs()
    recovered = webhook_queue._claim_next_job()
    assert recovered is not None
    job_id, provider, payload, _attempts, claimed_payload = recovered

    asyncio.run(webhook_queue._process_job(provider, payload))
    webhook_queue._complete_job(job_id, claimed_payload)

    assert seen == {"email_address": "person@example.com", "history_id": "99"}
    with factory() as db:
        assert db.query(WebhookDeliveryJob).count() == 0


def test_failed_job_is_retried_instead_of_lost(monkeypatch):
    factory = _session_factory()
    monkeypatch.setattr(webhook_queue, "SessionLocal", factory)
    webhook_queue.enqueue_webhook_delivery(
        "microsoft",
        {"subscription_id": "sub", "message_id": "message"},
        provider_id="sub:message",
    )
    claimed = webhook_queue._claim_next_job()
    assert claimed is not None

    webhook_queue._retry_job(claimed[0], claimed[3], RuntimeError("temporary outage"))

    with factory() as db:
        job = db.get(WebhookDeliveryJob, claimed[0])
        assert job is not None
        assert job.status == "pending"
        assert job.claimed_at is None
        assert job.last_error == "temporary outage"


def test_gmail_burst_keeps_latest_history_without_losing_inflight_job(monkeypatch):
    factory = _session_factory()
    monkeypatch.setattr(webhook_queue, "SessionLocal", factory)
    first = {"email_address": "person@example.com", "history_id": "100"}
    latest = {"email_address": "person@example.com", "history_id": "105"}

    assert webhook_queue.enqueue_webhook_delivery(
        "google", first, provider_id="person@example.com"
    )
    claimed = webhook_queue._claim_next_job()
    assert claimed is not None
    assert not webhook_queue.enqueue_webhook_delivery(
        "google", latest, provider_id="person@example.com"
    )

    webhook_queue._complete_job(claimed[0], claimed[4])
    next_claim = webhook_queue._claim_next_job()
    assert next_claim is not None
    assert next_claim[2] == latest
