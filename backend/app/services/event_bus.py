"""Durable domain/activity events shared by HTTP and worker processes.

The database is the source of truth. RabbitMQ publication is performed from the
transactional outbox, so a broker outage never loses an accepted event.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ActivityEvent, MailboxConnection, OutboxEvent

DOMAIN_TOPIC_BY_ACTIVITY = {
    "email.classified": "email.classified.v1",
    "calendar.changed": "interview.changed.v1",
    "mailbox.disconnected": "mailbox.disconnected.v1",
}


def topic(name: str) -> str:
    prefix = get_settings().rabbitmq_routing_prefix.strip().strip(".")
    return f"{prefix}.{name}" if prefix else name


def event_envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    user_id: int | None = None,
    aggregate_id: str = "",
    event_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    producer: str = "email-check-api",
) -> dict[str, Any]:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "producer": producer,
        "user_id": user_id,
        "aggregate_id": aggregate_id,
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "causation_id": causation_id,
        "payload": payload,
    }


def enqueue(
    db: Session,
    topic_name: str,
    partition_key: str,
    envelope: dict[str, Any],
) -> OutboxEvent:
    row = OutboxEvent(
        event_id=str(envelope["event_id"]),
        topic=topic(topic_name),
        partition_key=str(partition_key),
        payload=json.dumps(envelope, default=str, separators=(",", ":")),
        status="pending",
    )
    db.add(row)
    return row


def resolve_user_id(db: Session, data: dict[str, Any]) -> int | None:
    value = data.get("user_id")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    mailbox_id = data.get("mailbox_id")
    if mailbox_id is None:
        return None
    try:
        return db.query(MailboxConnection.user_id).filter(
            MailboxConnection.id == int(mailbox_id)
        ).scalar()
    except (TypeError, ValueError):
        return None


def persist_activity(db: Session, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Store a reconnectable activity and, when enabled, its broker outbox event."""
    user_id = resolve_user_id(db, data)
    envelope = event_envelope(
        event_type,
        data,
        user_id=user_id,
        aggregate_id=str(data.get("email_id") or data.get("event_id") or data.get("mailbox_id") or ""),
    )
    if user_id is None:
        return envelope

    activity = ActivityEvent(
        event_id=envelope["event_id"],
        user_id=user_id,
        event_type=event_type,
        payload=json.dumps(data, default=str, separators=(",", ":")),
    )
    db.add(activity)
    db.flush()
    envelope["sequence"] = activity.id
    if get_settings().rabbitmq_enabled:
        enqueue(db, "user.activity.v1", str(user_id), envelope)
        domain_topic = DOMAIN_TOPIC_BY_ACTIVITY.get(event_type)
        if domain_topic:
            domain_envelope = event_envelope(
                domain_topic,
                data,
                user_id=user_id,
                aggregate_id=envelope["aggregate_id"],
                correlation_id=envelope["correlation_id"],
                causation_id=envelope["event_id"],
                producer="email-check-domain",
            )
            enqueue(db, domain_topic, str(data.get("mailbox_id") or user_id), domain_envelope)
    return envelope

