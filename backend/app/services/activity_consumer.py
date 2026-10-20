"""Broadcast delivery-ready notifications to clients on this API replica."""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import get_settings
from app.realtime.sse import broadcast_envelope
from app.services.event_bus import topic

logger = logging.getLogger(__name__)


def client_notification(envelope: dict) -> dict:
    """Translate the notification-service contract to the browser contract."""
    delivery = envelope.get("payload") or {}
    return {
        "event": delivery.get("event_type") or "message",
        "event_id": delivery.get("source_event_id"),
        "notification_id": delivery.get("notification_id"),
        "sequence": delivery.get("sequence"),
        "user_id": envelope.get("user_id"),
        "data": delivery.get("data") or {},
    }


async def activity_consumer_loop(stop_event: asyncio.Event) -> None:
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
    # Every API replica needs a transient subscription. Reconnect recovery is
    # served from PostgreSQL's durable ActivityEvent records.
    queue = await channel.declare_queue("", exclusive=True, auto_delete=True, robust=False)
    await queue.bind(exchange, routing_key=topic("notification.ready.v1"))
    logger.info("RabbitMQ notification-to-client gateway started")
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
                    await broadcast_envelope(client_notification(envelope))
                    await message.ack()
                except Exception:
                    logger.exception("Invalid activity event")
                    await message.reject(requeue=False)
    finally:
        await connection.close()
