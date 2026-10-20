"""RabbitMQ consumer that turns imported-email events into AI enrichment work."""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import get_settings
from app.db import SessionLocal
from app.models import ConsumedEvent
from app.services.event_bus import topic
from app.services.reclassify import get_reclassify_status, start_mailbox_reclassify

logger = logging.getLogger(__name__)
CONSUMER_NAME = "email-classifiers-v1"


async def _classify_mailbox(mailbox_id: int) -> None:
    await start_mailbox_reclassify(mailbox_id, pending_only=True)
    while True:
        state = get_reclassify_status(mailbox_id)
        if state.get("state") != "running":
            if state.get("state") == "error" or state.get("failed", 0):
                raise RuntimeError(state.get("message") or "Email classification failed")
            return
        await asyncio.sleep(0.5)


def _already_consumed(event_id: str) -> bool:
    with SessionLocal() as db:
        return db.query(ConsumedEvent.id).filter(
            ConsumedEvent.consumer == CONSUMER_NAME,
            ConsumedEvent.event_id == event_id,
        ).first() is not None


def _mark_consumed(event_id: str) -> None:
    with SessionLocal() as db:
        if db.query(ConsumedEvent.id).filter(
            ConsumedEvent.consumer == CONSUMER_NAME,
            ConsumedEvent.event_id == event_id,
        ).first() is None:
            db.add(ConsumedEvent(consumer=CONSUMER_NAME, event_id=event_id))
            db.commit()


async def email_event_consumer_loop(stop_event: asyncio.Event) -> None:
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
    queue = await channel.declare_queue(
        queue_name, durable=True
    )
    await queue.bind(exchange, routing_key=topic("email.imported.v1"))
    logger.info("Email RabbitMQ consumer started")
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
                    event_id = str(envelope["event_id"])
                    if not _already_consumed(event_id):
                        payload = envelope.get("payload") or {}
                        if payload.get("classification_pending", True):
                            await _classify_mailbox(int(payload["mailbox_id"]))
                        _mark_consumed(event_id)
                    await message.ack()
                except Exception:
                    logger.exception("Could not process imported-email event")
                    await message.nack(requeue=True)
                    await asyncio.sleep(2)
    finally:
        await connection.close()
        logger.info("Email RabbitMQ consumer stopped")
