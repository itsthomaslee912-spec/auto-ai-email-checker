from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import Provider, WebhookDeliveryJob
from app.services.mailbox_sync import process_gmail_notification, process_outlook_notification

logger = logging.getLogger(__name__)
_wake_event = asyncio.Event()
WEBHOOK_JOB_TIMEOUT_SECONDS = 300
WEBHOOK_WORKER_CONCURRENCY = 4


def _dedupe_key(provider: str, provider_id: str | None, payload: dict) -> str:
    stable_id = (provider_id or "").strip()
    if not stable_id:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        stable_id = sha256(encoded).hexdigest()
    return f"{provider}:{stable_id}"[:255]


def enqueue_webhook_delivery(
    provider: str, payload: dict, *, provider_id: str | None = None,
) -> bool:
    """Commit a notification before its HTTP request is acknowledged.

    Returns False for a provider retry that is already waiting in the queue.
    """
    encoded_payload = json.dumps(payload, separators=(",", ":"))
    dedupe_key = _dedupe_key(provider, provider_id, payload)
    job = WebhookDeliveryJob(
        provider=provider,
        dedupe_key=dedupe_key,
        payload=encoded_payload,
        status="pending",
        available_at=datetime.now(timezone.utc),
    )
    with SessionLocal() as db:
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.query(WebhookDeliveryJob).filter(
                WebhookDeliveryJob.dedupe_key == dedupe_key
            ).one_or_none()
            if existing is not None:
                # If the worker already claimed the older payload, completion
                # compares payloads and puts this newer revision back in the
                # pending state instead of deleting it.
                existing.payload = encoded_payload
                existing.last_error = None
                if existing.status != "processing":
                    existing.status = "pending"
                    existing.available_at = datetime.now(timezone.utc)
                db.commit()
            return False
    _wake_event.set()
    return True


def _recover_interrupted_jobs() -> None:
    with SessionLocal() as db:
        jobs = db.query(WebhookDeliveryJob).filter(
            WebhookDeliveryJob.status == "processing"
        ).all()
        for job in jobs:
            job.status = "pending"
            job.claimed_at = None
            job.available_at = datetime.now(timezone.utc)
        if jobs:
            db.commit()
            logger.warning("Recovered %s interrupted webhook job(s)", len(jobs))


def _claim_next_job() -> tuple[int, str, dict, int, str] | None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        job = (
            db.query(WebhookDeliveryJob)
            .filter(
                WebhookDeliveryJob.status == "pending",
                WebhookDeliveryJob.available_at <= now,
            )
            .order_by(WebhookDeliveryJob.id)
            .with_for_update(skip_locked=True)
            .first()
        )
        if job is None:
            return None
        job.status = "processing"
        job.claimed_at = now
        job.attempts += 1
        db.commit()
        return job.id, job.provider, json.loads(job.payload), job.attempts, job.payload


async def _process_job(provider: str, payload: dict) -> None:
    with SessionLocal() as db:
        if provider == Provider.GOOGLE.value:
            await process_gmail_notification(
                db, str(payload.get("email_address") or ""), payload.get("history_id")
            )
            return
        if provider == Provider.MICROSOFT.value:
            await process_outlook_notification(
                db,
                str(payload.get("subscription_id") or ""),
                str(payload.get("message_id") or ""),
            )
            return
        raise ValueError(f"Unsupported webhook provider: {provider}")


def _complete_job(job_id: int, claimed_payload: str) -> None:
    with SessionLocal() as db:
        job = db.get(WebhookDeliveryJob, job_id)
        if job is not None:
            if job.payload == claimed_payload:
                db.delete(job)
            else:
                job.status = "pending"
                job.claimed_at = None
                job.available_at = datetime.now(timezone.utc)
            db.commit()


def _retry_job(job_id: int, attempts: int, exc: Exception) -> None:
    delay = min(300, max(2, 2 ** min(attempts, 8)))
    with SessionLocal() as db:
        job = db.get(WebhookDeliveryJob, job_id)
        if job is None:
            return
        job.status = "pending"
        job.claimed_at = None
        job.available_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        job.last_error = str(exc)[:512]
        db.commit()


async def _webhook_worker(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        claimed = await asyncio.to_thread(_claim_next_job)
        if claimed is not None:
            job_id, provider, payload, attempts, claimed_payload = claimed
            try:
                await asyncio.wait_for(
                    _process_job(provider, payload),
                    timeout=WEBHOOK_JOB_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                logger.error(
                    "Webhook job %s exceeded %ss; scheduling retry",
                    job_id,
                    WEBHOOK_JOB_TIMEOUT_SECONDS,
                )
                await asyncio.to_thread(_retry_job, job_id, attempts, exc)
            except Exception as exc:
                logger.exception("Webhook job %s failed; scheduling retry", job_id)
                await asyncio.to_thread(_retry_job, job_id, attempts, exc)
            else:
                await asyncio.to_thread(_complete_job, job_id, claimed_payload)
            continue

        _wake_event.clear()
        try:
            await asyncio.wait_for(_wake_event.wait(), timeout=2)
        except TimeoutError:
            pass


async def webhook_queue_loop(stop_event: asyncio.Event) -> None:
    """Drain durable provider notifications without one account blocking all others."""
    await asyncio.to_thread(_recover_interrupted_jobs)
    workers = [
        asyncio.create_task(_webhook_worker(stop_event))
        for _ in range(WEBHOOK_WORKER_CONCURRENCY)
    ]
    try:
        await asyncio.gather(*workers)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
