from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import ConsumedEvent
from app.services import email_event_consumer


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ConsumedEvent.__table__.create(engine)
    return sessionmaker(bind=engine)


def test_consumed_event_marker_is_idempotent(monkeypatch):
    sessions = _session_factory()
    monkeypatch.setattr(email_event_consumer, "SessionLocal", sessions)

    assert email_event_consumer._already_consumed("event-1") is False

    email_event_consumer._mark_consumed("event-1")
    email_event_consumer._mark_consumed("event-1")

    assert email_event_consumer._already_consumed("event-1") is True
    with sessions() as db:
        assert db.query(ConsumedEvent).count() == 1
