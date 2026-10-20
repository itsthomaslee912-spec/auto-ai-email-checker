"""Kafka trigger that turns imported-email events into AI enrichment work."""
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


def _consumer_config() -> dict[str, str]:
    settings = get_settings()
    config = {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "group.id": CONSUMER_NAME,
        "client.id": f"{settings.kafka_client_id}-classifier",
        "enable.auto.commit": "false",
        "auto.offset.reset": "earliest",
        "security.protocol": settings.kafka_security_protocol,
    }
    if settings.kafka_sasl_mechanism:
        config.update({
            "sasl.mechanism": settings.kafka_sasl_mechanism,
            "sasl.username": settings.kafka_sasl_username,
            "sasl.password": settings.kafka_sasl_password,
        })
    return config


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
    if not settings.kafka_enabled:
        await stop_event.wait()
        return

    from confluent_kafka import Consumer, KafkaError

    consumer = Consumer(_consumer_config())
    consumer.subscribe([topic("email.imported.v1")])
    logger.info("Email Kafka consumer started")
    try:
        while not stop_event.is_set():
            message = await asyncio.to_thread(consumer.poll, 1.0)
            if message is None:
                continue
            if message.error():
                if message.error().code() != KafkaError._PARTITION_EOF:
                    logger.warning("Kafka consumer error: %s", message.error())
                continue
            try:
                envelope = json.loads(message.value().decode("utf-8"))
                event_id = str(envelope["event_id"])
                if _already_consumed(event_id):
                    await asyncio.to_thread(consumer.commit, message, False)
                    continue
                payload = envelope.get("payload") or {}
                mailbox_id = int(payload["mailbox_id"])
                if payload.get("classification_pending", True):
                    await _classify_mailbox(mailbox_id)
                _mark_consumed(event_id)
                await asyncio.to_thread(consumer.commit, message, False)
            except Exception:
                logger.exception("Could not process imported-email event")
                await asyncio.sleep(2)
    finally:
        await asyncio.to_thread(consumer.close)
        logger.info("Email Kafka consumer stopped")
