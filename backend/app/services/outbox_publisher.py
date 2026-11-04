"""RabbitMQ publisher for durable transactional-outbox rows."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import ConsumedEvent, OutboxEvent
from app.services.event_bus import topic
from app.services.service_health import set_component_health

logger = logging.getLogger(__name__)
CONSUMER_QUEUE = "email-classifiers-v1"
NOTIFICATION_CONSUMER = "notification-service-v1"
_HEALTH_COMPONENT = "rabbitmq-publisher"
_RECONNECT_BACKOFF_CAP_SECONDS = 30


class BrokerPublishError(RuntimeError):
    """The current publisher channel cannot safely be reused."""


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
        # A broker can route a message to a fast consumer and still return a
        # publisher NACK while a queue is recovering. The notification inbox is
        # authoritative proof that delivery completed, so reconcile those rows
        # instead of retrying them forever and reporting a false degradation.
        consumed_activity_ids = select(ConsumedEvent.event_id).where(
            ConsumedEvent.consumer == NOTIFICATION_CONSUMER
        )
        reconciled = list(db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.status == "failed",
                OutboxEvent.topic == topic("user.activity.v1"),
                OutboxEvent.event_id.in_(consumed_activity_ids),
            )
        ))
        for row in reconciled:
            row.status = "published"
            row.published_at = now
            row.last_error = None
        if reconciled:
            # SessionLocal disables autoflush, so make the new status visible to
            # the claim query in this same transaction.
            db.flush()
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
    broker_error: Exception | None = None
    for event in events:
        if broker_error is not None:
            failures[event.id] = f"Publish batch aborted after broker error: {broker_error}"
            continue
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
            broker_error = exc
    published = await asyncio.to_thread(_finish_batch, events, failures)
    if broker_error is not None:
        # A NACK or channel failure invalidates publisher-confirm state for the
        # whole channel. Reusing that exchange caused every retry to be NACKed
        # until its exponential backoff reached an hour.
        raise BrokerPublishError(str(broker_error)) from broker_error
    return published


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

    backoff = 1.0
    while not stop_event.is_set():
        connection = None
        try:
            connection, exchange = await _open_broker()
            set_component_health(_HEALTH_COMPONENT, "healthy")
            logger.info("RabbitMQ outbox publisher started")
            backoff = 1.0
            while not stop_event.is_set():
                count = await _publish_batch(exchange)
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
        except Exception as error:
            if stop_event.is_set():
                break
            set_component_health(_HEALTH_COMPONENT, "recovering")
            logger.warning(
                "RabbitMQ publisher lost its broker channel (%s); reconnecting",
                error,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                break
            except TimeoutError:
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_CAP_SECONDS)
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except Exception:
                    logger.debug("Could not close RabbitMQ publisher connection", exc_info=True)
    set_component_health(_HEALTH_COMPONENT, "healthy")
