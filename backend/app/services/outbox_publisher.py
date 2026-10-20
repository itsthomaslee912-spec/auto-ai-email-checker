"""Kafka publisher for durable transactional-outbox rows."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import OutboxEvent

logger = logging.getLogger(__name__)


def _producer_config() -> dict[str, str]:
    settings = get_settings()
    config = {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "client.id": settings.kafka_client_id,
        "enable.idempotence": "true",
        "acks": "all",
        "security.protocol": settings.kafka_security_protocol,
    }
    if settings.kafka_sasl_mechanism:
        config.update({
            "sasl.mechanism": settings.kafka_sasl_mechanism,
            "sasl.username": settings.kafka_sasl_username,
            "sasl.password": settings.kafka_sasl_password,
        })
    return config


def _publish_batch() -> int:
    settings = get_settings()
    if not settings.kafka_enabled:
        return 0

    from confluent_kafka import Producer

    producer = Producer(_producer_config())
    now = datetime.now(timezone.utc)
    stale_claim = now - timedelta(minutes=5)
    published = 0
    with SessionLocal() as db:
        stmt = (
            select(OutboxEvent)
            .where(
                or_(
                    and_(
                        OutboxEvent.status.in_(("pending", "failed")),
                        OutboxEvent.available_at <= now,
                    ),
                    and_(
                        OutboxEvent.status == "publishing",
                        OutboxEvent.claimed_at <= stale_claim,
                    ),
                )
            )
            .order_by(OutboxEvent.id)
            .limit(max(1, settings.kafka_outbox_batch_size))
        )
        if db.bind and db.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        rows = list(db.scalars(stmt))
        for row in rows:
            row.status = "publishing"
            row.claimed_at = now
            row.attempts += 1
        db.commit()

        for row in rows:
            error: list[str] = []

            def delivered(err, _message) -> None:
                if err is not None:
                    error.append(str(err))

            try:
                producer.produce(
                    row.topic,
                    key=row.partition_key.encode("utf-8"),
                    value=row.payload.encode("utf-8"),
                    on_delivery=delivered,
                )
                producer.flush(10)
                current = db.get(OutboxEvent, row.id)
                if current is None:
                    continue
                if error:
                    raise RuntimeError(error[0])
                current.status = "published"
                current.published_at = datetime.now(timezone.utc)
                current.last_error = None
                published += 1
            except Exception as exc:
                current = db.get(OutboxEvent, row.id)
                if current is not None:
                    current.status = "failed"
                    current.last_error = str(exc)[:512]
                    delay = min(3600, 2 ** min(current.attempts, 10))
                    current.available_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                logger.warning("Could not publish outbox event %s: %s", row.event_id, exc)
            db.commit()
    return published


async def outbox_publisher_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    if not settings.kafka_enabled:
        logger.info("Kafka outbox publisher disabled: KAFKA_BOOTSTRAP_SERVERS is empty")
        await stop_event.wait()
        return
    logger.info("Kafka outbox publisher started")
    while not stop_event.is_set():
        try:
            count = await asyncio.to_thread(_publish_batch)
        except Exception:
            count = 0
            logger.exception("Kafka outbox publisher failed")
        if count:
            await asyncio.sleep(0)
            continue
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=max(0.1, settings.kafka_outbox_poll_seconds)
            )
        except TimeoutError:
            pass

