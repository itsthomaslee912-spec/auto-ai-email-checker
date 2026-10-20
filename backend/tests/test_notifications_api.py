from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.notifications import (
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)
from app.db import Base
from app.models import Notification, User


def _database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_notification_history_and_read_state_are_user_scoped():
    session_factory = _database()
    with session_factory() as db:
        first = User(external_id="first", email="first@example.com")
        second = User(external_id="second", email="second@example.com")
        db.add_all((first, second))
        db.flush()
        rows = [
            Notification(
                notification_id="notice-1",
                source_event_id="source-1",
                user_id=first.id,
                event_type="email.classified",
                payload=json.dumps({"email_id": 1}),
            ),
            Notification(
                notification_id="notice-2",
                source_event_id="source-2",
                user_id=first.id,
                event_type="calendar.changed",
                payload=json.dumps({"event_id": 2}),
            ),
            Notification(
                notification_id="notice-other",
                source_event_id="source-other",
                user_id=second.id,
                event_type="email.classified",
                payload="{}",
            ),
        ]
        db.add_all(rows)
        db.commit()

        page = list_notifications(False, 100, first.id, db)
        assert [item["id"] for item in page["items"]] == ["notice-2", "notice-1"]
        assert page["unread_count"] == 2

        marked = mark_notification_read("notice-1", first.id, db)
        assert marked["is_read"] is True

        result = mark_all_notifications_read(first.id, db)
        assert result == {"marked": 1}
        assert list_notifications(True, 100, first.id, db)["items"] == []
        assert list_notifications(True, 100, second.id, db)["unread_count"] == 1
