from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import ConsumedEvent, Notification, OutboxEvent, User
from app.services import activity_consumer, notification_consumer


def _database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_notification_service_persists_and_routes_activity(monkeypatch):
    session_factory = _database()
    with session_factory() as db:
        user = User(external_id="notification-user", email="notify@example.com")
        db.add(user)
        db.commit()
        user_id = user.id

    monkeypatch.setattr(notification_consumer, "SessionLocal", session_factory)
    envelope = {
        "event_id": "11111111-1111-1111-1111-111111111111",
        "event_type": "email.classified",
        "user_id": user_id,
        "sequence": 42,
        "correlation_id": "22222222-2222-2222-2222-222222222222",
        "payload": {"email_id": 7, "mailbox_id": 3},
    }

    assert notification_consumer.persist_notification(envelope) is True
    assert notification_consumer.persist_notification(envelope) is False

    with session_factory() as db:
        notification = db.query(Notification).one()
        outbox = db.query(OutboxEvent).one()
        consumed = db.query(ConsumedEvent).one()

    assert notification.source_event_id == envelope["event_id"]
    assert notification.user_id == user_id
    assert notification.event_type == "email.classified"
    assert notification.is_read is False
    assert outbox.topic.endswith("notification.ready.v1")
    assert consumed.consumer == notification_consumer.CONSUMER_NAME


def test_notification_service_ignores_events_without_user():
    assert notification_consumer.persist_notification({
        "event_id": "33333333-3333-3333-3333-333333333333",
        "event_type": "email.classified",
        "payload": {},
    }) is False


def test_notification_delivery_preserves_browser_event_contract():
    client_event = activity_consumer.client_notification({
        "user_id": 5,
        "payload": {
            "notification_id": "notice-1",
            "source_event_id": "source-1",
            "event_type": "calendar.changed",
            "sequence": 12,
            "data": {"mailbox_id": 8},
        },
    })

    assert client_event == {
        "event": "calendar.changed",
        "event_id": "source-1",
        "notification_id": "notice-1",
        "sequence": 12,
        "user_id": 5,
        "data": {"mailbox_id": 8},
    }
