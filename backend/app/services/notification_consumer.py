"""Turn user activity events into independently deliverable notifications."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.models import ConsumedEvent, Notification
from app.services.event_bus import enqueue, event_envelope, topic

logger = logging.getLogger(__name__)
CONSUMER_NAME = "notification-service-v1"
READY_EVENT = "notification.ready.v1"


def persist_notification(envelope: dict) -> bool:
    """Persist one in-app notification and enqueue its delivery event atomically."""
    source_event_id = str(envelope["event_id"])
    user_id = envelope.get("user_id")
    if user_id is None:
        logger.warning("Ignoring activity event %s without a user_id", source_event_id)
        return False

    payload = envelope.get("payload") or {}
    event_type = str(envelope.get("event_type") or "message")
    notification_id = str(uuid.uuid4())

    with SessionLocal() as db:
        try:
            if db.query(ConsumedEvent.id).filter(
                ConsumedEvent.consumer == CONSUMER_NAME,
                ConsumedEvent.event_id == source_event_id,
            ).first() is not None:
                return False

            notification = Notification(
                notification_id=notification_id,
                source_event_id=source_event_id,
                user_id=int(user_id),
                event_type=event_type,
                channel="in_app",
                payload=json.dumps(payload, default=str, separators=(",", ":")),
            )
            db.add(notification)
            db.flush()

            delivery_payload = {
                "notification_id": notification_id,
                "source_event_id": source_event_id,
                "event_type": event_type,
                "sequence": envelope.get("sequence"),
                "data": payload,
                "channel": "in_app",
            }
            ready = event_envelope(
                READY_EVENT,
                delivery_payload,
                user_id=int(user_id),
                aggregate_id=str(notification.id),
                correlation_id=envelope.get("correlation_id"),
                causation_id=source_event_id,
                producer="notification-service",
            )
            enqueue(db, READY_EVENT, str(user_id), ready)
            db.add(ConsumedEvent(consumer=CONSUMER_NAME, event_id=source_event_id))
            db.commit()
        except IntegrityError:
            # Another replica won the idempotency race. Its transaction owns
            # both the notification and the delivery outbox event.
            db.rollback()
            return False
    return True


async def notification_consumer_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    if not settings.rabbitmq_enabled:
        await stop_event.wait()
        return

    import aio_pika

    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=max(1, settings.rabbitmq_prefetch_count))
    exchange = await channel.declare_exchange(
        settings.rabbitmq_exchange, aio_pika.ExchangeType.TOPIC, durable=True
    )
    prefix = settings.rabbitmq_routing_prefix.strip().strip(".")
    queue_name = f"{prefix}.{CONSUMER_NAME}" if prefix else CONSUMER_NAME
    queue = await channel.declare_queue(queue_name, durable=True)
    await queue.bind(exchange, routing_key=topic("user.activity.v1"))
    logger.info("Notification service RabbitMQ consumer started")
    try:
        async with queue.iterator() as messages:
            while not stop_event.is_set():
                next_message = asyncio.create_task(anext(messages))
                stopped = asyncio.create_task(stop_event.wait())
                done, pending = await asyncio.wait(
                    (next_message, stopped), return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stopped in done:
                    break
                message = next_message.result()
                try:
                    envelope = json.loads(message.body.decode("utf-8"))
                    await asyncio.to_thread(persist_notification, envelope)
                    await message.ack()
                except Exception:
                    logger.exception("Could not prepare notification")
                    await message.nack(requeue=True)
                    await asyncio.sleep(2)
    finally:
        await connection.close()
        logger.info("Notification service RabbitMQ consumer stopped")
