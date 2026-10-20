"""Broadcast Kafka user-activity events to WebSockets connected to this API replica."""
from __future__ import annotations

import asyncio
import json
import logging
import socket

from app.config import get_settings
from app.realtime.sse import broadcast_envelope
from app.services.event_bus import topic

logger = logging.getLogger(__name__)


def _config() -> dict[str, str]:
    settings = get_settings()
    instance = socket.gethostname()
    config = {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        # Every API replica needs every activity event for its own sockets.
        "group.id": f"api-ui-{instance}",
        "client.id": f"{settings.kafka_client_id}-api-{instance}",
        "enable.auto.commit": "true",
        "auto.offset.reset": "latest",
        "security.protocol": settings.kafka_security_protocol,
    }
    if settings.kafka_sasl_mechanism:
        config.update({
            "sasl.mechanism": settings.kafka_sasl_mechanism,
            "sasl.username": settings.kafka_sasl_username,
            "sasl.password": settings.kafka_sasl_password,
        })
    return config


async def activity_consumer_loop(stop_event: asyncio.Event) -> None:
    if not get_settings().kafka_enabled:
        await stop_event.wait()
        return
    from confluent_kafka import Consumer, KafkaError

    consumer = Consumer(_config())
    consumer.subscribe([topic("user.activity.v1")])
    logger.info("Kafka activity-to-WebSocket bridge started")
    try:
        while not stop_event.is_set():
            message = await asyncio.to_thread(consumer.poll, 1.0)
            if message is None:
                continue
            if message.error():
                if message.error().code() != KafkaError._PARTITION_EOF:
                    logger.warning("Activity consumer error: %s", message.error())
                continue
            try:
                envelope = json.loads(message.value().decode("utf-8"))
                await broadcast_envelope({
                    "event": envelope.get("event_type") or "message",
                    "event_id": envelope.get("event_id"),
                    "sequence": envelope.get("sequence"),
                    "user_id": envelope.get("user_id"),
                    "data": envelope.get("payload") or {},
                })
            except Exception:
                logger.exception("Invalid activity event")
    finally:
        await asyncio.to_thread(consumer.close)
