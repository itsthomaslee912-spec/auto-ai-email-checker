import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import ConsumedEvent, OutboxEvent
from app.services import outbox_publisher


class _Message:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Exchange:
    def __init__(self):
        self.delivered = []

    async def publish(self, message, *, routing_key):
        self.delivered.append((routing_key, message))


class _FailingExchange(_Exchange):
    async def publish(self, message, *, routing_key):
        self.delivered.append((routing_key, message))
        raise RuntimeError("publisher NACK")


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    OutboxEvent.__table__.create(engine)
    ConsumedEvent.__table__.create(engine)
    return sessionmaker(bind=engine)


def test_publisher_recovers_stale_claim_but_not_active_claim(monkeypatch):
    sessions = _session_factory()
    now = datetime.now(timezone.utc)
    with sessions() as db:
        db.add_all([
            OutboxEvent(
                event_id="pending-event",
                topic="test.events.v1",
                partition_key="1",
                payload="{}",
                status="pending",
                available_at=now - timedelta(seconds=1),
            ),
            OutboxEvent(
                event_id="stale-event",
                topic="test.events.v1",
                partition_key="1",
                payload="{}",
                status="publishing",
                claimed_at=now - timedelta(minutes=10),
            ),
            OutboxEvent(
                event_id="active-event",
                topic="test.events.v1",
                partition_key="1",
                payload="{}",
                status="publishing",
                claimed_at=now,
            ),
        ])
        db.commit()

    settings = SimpleNamespace(
        rabbitmq_enabled=True,
        rabbitmq_outbox_batch_size=100,
    )
    monkeypatch.setattr(outbox_publisher, "SessionLocal", sessions)
    monkeypatch.setattr(outbox_publisher, "get_settings", lambda: settings)
    monkeypatch.setitem(sys.modules, "aio_pika", SimpleNamespace(
        Message=_Message,
        DeliveryMode=SimpleNamespace(PERSISTENT=2),
    ))
    exchange = _Exchange()

    assert asyncio.run(outbox_publisher._publish_batch(exchange)) == 2
    assert len(exchange.delivered) == 2

    with sessions() as db:
        statuses = {
            item.event_id: item.status
            for item in db.query(OutboxEvent).order_by(OutboxEvent.id)
        }
    assert statuses == {
        "pending-event": "published",
        "stale-event": "published",
        "active-event": "publishing",
    }


def test_publisher_aborts_batch_and_requires_fresh_channel(monkeypatch):
    sessions = _session_factory()
    now = datetime.now(timezone.utc)
    with sessions() as db:
        db.add_all([
            OutboxEvent(
                event_id=f"event-{index}",
                topic="test.events.v1",
                partition_key="1",
                payload="{}",
                status="pending",
                available_at=now - timedelta(seconds=1),
            )
            for index in range(3)
        ])
        db.commit()

    settings = SimpleNamespace(rabbitmq_enabled=True, rabbitmq_outbox_batch_size=100)
    monkeypatch.setattr(outbox_publisher, "SessionLocal", sessions)
    monkeypatch.setattr(outbox_publisher, "get_settings", lambda: settings)
    monkeypatch.setitem(sys.modules, "aio_pika", SimpleNamespace(
        Message=_Message,
        DeliveryMode=SimpleNamespace(PERSISTENT=2),
    ))
    exchange = _FailingExchange()

    try:
        asyncio.run(outbox_publisher._publish_batch(exchange))
        assert False, "publisher failure must rebuild the channel"
    except outbox_publisher.BrokerPublishError:
        pass

    # Stop using the invalid channel after its first NACK.
    assert len(exchange.delivered) == 1
    with sessions() as db:
        rows = db.query(OutboxEvent).order_by(OutboxEvent.id).all()
        assert all(row.status == "failed" for row in rows)
        assert "publisher NACK" in rows[0].last_error
        assert "batch aborted" in rows[1].last_error.lower()


def test_claim_reconciles_activity_event_already_consumed(monkeypatch):
    sessions = _session_factory()
    now = datetime.now(timezone.utc)
    activity_topic = outbox_publisher.topic("user.activity.v1")
    with sessions() as db:
        db.add(OutboxEvent(
            event_id="already-consumed",
            topic=activity_topic,
            partition_key="1",
            payload="{}",
            status="failed",
            available_at=now - timedelta(seconds=1),
            last_error="publisher NACK",
        ))
        db.add(ConsumedEvent(
            consumer=outbox_publisher.NOTIFICATION_CONSUMER,
            event_id="already-consumed",
        ))
        db.commit()

    settings = SimpleNamespace(
        rabbitmq_enabled=True,
        rabbitmq_outbox_batch_size=100,
    )
    monkeypatch.setattr(outbox_publisher, "SessionLocal", sessions)
    monkeypatch.setattr(outbox_publisher, "get_settings", lambda: settings)

    assert outbox_publisher._claim_batch() == []
    with sessions() as db:
        row = db.query(OutboxEvent).one()
        assert row.status == "published"
        assert row.last_error is None
        assert row.published_at is not None
