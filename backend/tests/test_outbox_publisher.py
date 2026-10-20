import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import OutboxEvent
from app.services import outbox_publisher


class _Producer:
    def __init__(self, _config):
        self.delivered = []

    def produce(self, topic, *, key, value, on_delivery):
        self.delivered.append((topic, key, value))
        on_delivery(None, None)

    def flush(self, _timeout):
        return 0


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    OutboxEvent.__table__.create(engine)
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
        kafka_enabled=True,
        kafka_bootstrap_servers="kafka:29092",
        kafka_client_id="test",
        kafka_security_protocol="PLAINTEXT",
        kafka_sasl_mechanism="",
        kafka_sasl_username="",
        kafka_sasl_password="",
        kafka_outbox_batch_size=100,
    )
    monkeypatch.setattr(outbox_publisher, "SessionLocal", sessions)
    monkeypatch.setattr(outbox_publisher, "get_settings", lambda: settings)
    monkeypatch.setitem(sys.modules, "confluent_kafka", SimpleNamespace(Producer=_Producer))

    assert outbox_publisher._publish_batch() == 2

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
