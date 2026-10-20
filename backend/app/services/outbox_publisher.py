"""RabbitMQ publisher for durable transactional-outbox rows."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import OutboxEvent
from app.services.event_bus import topic

logger = logging.getLogger(__name__)
CONSUMER_QUEUE = "email-classifiers-v1"


@dataclass(frozen=True)
class ClaimedEvent:
    id: int
    event_id: str
    routing_key: str
    payload: str


def _claim_batch() -> list[ClaimedEvent]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    stale_claim = now - timedelta(minutes=5)
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
            .limit(max(1, settings.rabbitmq_outbox_batch_size))
        )
        if db.bind and db.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        rows = list(db.scalars(stmt))
        claimed = [ClaimedEvent(row.id, row.event_id, row.topic, row.payload) for row in rows]
        for row in rows:
            row.status = "publishing"
            row.claimed_at = now
            row.attempts += 1
        db.commit()
        return claimed


def _finish_batch(events: list[ClaimedEvent], failures: dict[int, str]) -> int:
    published = 0
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for event in events:
            row = db.get(OutboxEvent, event.id)
            if row is None:
                continue
            error = failures.get(event.id)
            if error is None:
                row.status = "published"
                row.published_at = now
                row.last_error = None
                published += 1
                continue
            row.status = "failed"
            row.last_error = error[:512]
            delay = min(3600, 2 ** min(row.attempts, 10))
            row.available_at = now + timedelta(seconds=delay)
            logger.warning("Could not publish outbox event %s: %s", event.event_id, error)
        db.commit()
    return published


async def _publish_batch(exchange) -> int:
    """Publish one claimed batch using RabbitMQ publisher confirmations."""
    from aio_pika import DeliveryMode, Message

    if not get_settings().rabbitmq_enabled:
        return 0
    events = await asyncio.to_thread(_claim_batch)
    failures: dict[int, str] = {}
    for event in events:
        try:
            await exchange.publish(
                Message(
                    body=event.payload.encode("utf-8"),
                    content_type="application/json",
                    delivery_mode=DeliveryMode.PERSISTENT,
                    message_id=event.event_id,
                ),
                routing_key=event.routing_key,
            )
        except Exception as exc:
            failures[event.id] = str(exc)
    return await asyncio.to_thread(_finish_batch, events, failures)


async def _open_broker():
    import aio_pika

    settings = get_settings()
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel(publisher_confirms=True)
    exchange = await channel.declare_exchange(
        settings.rabbitmq_exchange,
        aio_pika.ExchangeType.TOPIC,
        durable=True,
    )
    # Ensure the durable work queue exists before the first event is published.
    prefix = settings.rabbitmq_routing_prefix.strip().strip(".")
    queue_name = f"{prefix}.{CONSUMER_QUEUE}" if prefix else CONSUMER_QUEUE
    queue = await channel.declare_queue(
        queue_name,
        durable=True,
    )
    await queue.bind(exchange, routing_key=topic("email.imported.v1"))
    return connection, exchange


async def outbox_publisher_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    if not settings.rabbitmq_enabled:
        logger.info("RabbitMQ outbox publisher disabled: RABBITMQ_URL is empty")
        await stop_event.wait()
        return

    connection, exchange = await _open_broker()
    logger.info("RabbitMQ outbox publisher started")
    try:
        while not stop_event.is_set():
            try:
                count = await _publish_batch(exchange)
            except Exception:
                count = 0
                logger.exception("RabbitMQ outbox publisher failed")
            if count:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=max(0.05, settings.rabbitmq_outbox_poll_seconds),
                )
            except TimeoutError:
                pass
    finally:
        await connection.close()
