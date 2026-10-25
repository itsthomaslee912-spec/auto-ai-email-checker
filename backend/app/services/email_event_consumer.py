"""RabbitMQ consumer that turns imported-email events into AI enrichment work."""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import get_settings
from app.db import SessionLocal
from app.models import ConsumedEvent
from app.services.event_bus import topic
from app.services.reclassify import start_mailbox_reclassify
from app.services.service_health import set_component_health

logger = logging.getLogger(__name__)
CONSUMER_NAME = "email-classifiers-v1"
_HEALTH_COMPONENT = "email-consumer"
# When the queue holds work but no message is delivered within this window the
# consumer treats its channel as stalled and rebuilds it. A robust connection
# reconnects the socket but can leave the old queue iterator permanently idle,
# which previously stranded the backlog while the worker still looked healthy.
STALL_CHECK_SECONDS = 30
_RECONNECT_BACKOFF_CAP_SECONDS = 30


class _ConsumerStalled(RuntimeError):
    """Raised when the queue has messages but delivery has stopped."""


async def _classify_mailbox(mailbox_id: int) -> None:
    """Accept durable classification work without awaiting the whole AI pass.

    The email row's ``classification_pending`` flag is the durable work record.
    Holding a broker delivery open for a mailbox-wide pass caused healthy slow
    batches to time out and be requeued while the original pass kept running.
    """
    await start_mailbox_reclassify(mailbox_id, pending_only=True)


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


async def _handle_message(message) -> None:
    envelope = json.loads(message.body.decode("utf-8"))
    event_id = str(envelope["event_id"])
    if not _already_consumed(event_id):
        payload = envelope.get("payload") or {}
        if payload.get("classification_pending", True):
            await _classify_mailbox(int(payload["mailbox_id"]))
        _mark_consumed(event_id)
    await message.ack()


async def _consume_once(settings, stop_event: asyncio.Event) -> None:
    """Connect, consume, and return only on shutdown. Raise to force a rebuild."""
    import aio_pika

    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    try:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=max(1, settings.rabbitmq_prefetch_count))
        exchange = await channel.declare_exchange(
            settings.rabbitmq_exchange, aio_pika.ExchangeType.TOPIC, durable=True
        )
        prefix = settings.rabbitmq_routing_prefix.strip().strip(".")
        queue_name = f"{prefix}.{CONSUMER_NAME}" if prefix else CONSUMER_NAME
        queue = await channel.declare_queue(queue_name, durable=True)
        await queue.bind(exchange, routing_key=topic("email.imported.v1"))
        # Connected and bound: this replica is doing its job again.
        set_component_health(_HEALTH_COMPONENT, "healthy")
        logger.info("Email RabbitMQ consumer started")
        async with queue.iterator() as messages:
            while not stop_event.is_set():
                message = await _next_message(messages, channel, queue_name, stop_event)
                if message is None:
                    return
                try:
                    await _handle_message(message)
                except Exception:
                    logger.exception("Could not process imported-email event")
                    await message.nack(requeue=True)
                    await asyncio.sleep(2)
    finally:
        await connection.close()
        logger.info("Email RabbitMQ consumer stopped")


async def _next_message(messages, channel, queue_name: str, stop_event: asyncio.Event):
    """Wait for a delivery while retaining the broker read across idle probes.

    Cancelling ``QueueIterator.__anext__`` can unregister aio-pika's consumer.
    Keep one read alive while probing so an idle queue cannot detach itself.
    """
    delivery = asyncio.create_task(messages.__anext__())
    try:
        while True:
            stopped = asyncio.create_task(stop_event.wait())
            done, _pending = await asyncio.wait(
                (delivery, stopped),
                timeout=STALL_CHECK_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stopped in done:
                return None
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
            if delivery in done:
                try:
                    return delivery.result()
                except StopAsyncIteration:
                    raise _ConsumerStalled("Queue iterator closed; consumer detached")

            waiting = await _queue_depth(channel, queue_name)
            if waiting > 0:
                raise _ConsumerStalled(
                    f"{waiting} message(s) waiting but none delivered in {STALL_CHECK_SECONDS}s"
                )
    finally:
        if not delivery.done():
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)


async def _queue_depth(channel, queue_name: str) -> int:
    """Current ready-message count via a passive declare."""
    try:
        declared = await channel.declare_queue(queue_name, durable=True, passive=True)
        return int(getattr(declared.declaration_result, "message_count", 0) or 0)
    except Exception:
        # If we cannot even probe the queue the channel is unusable; treat it as
        # a stall so the connection is rebuilt.
        return 1


async def email_event_consumer_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    if not settings.rabbitmq_enabled:
        await stop_event.wait()
        return

    backoff = 1.0
    while not stop_event.is_set():
        try:
            await _consume_once(settings, stop_event)
            return  # clean return means shutdown was requested
        except Exception as error:
            if stop_event.is_set():
                return
            # Report the degraded state so the dashboard shows this replica
            # reconnecting instead of a misleadingly healthy worker.
            set_component_health(_HEALTH_COMPONENT, "recovering")
            logger.warning("Email consumer lost its broker channel (%s); reconnecting", error)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                return
            except TimeoutError:
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_CAP_SECONDS)
    set_component_health(_HEALTH_COMPONENT, "healthy")
