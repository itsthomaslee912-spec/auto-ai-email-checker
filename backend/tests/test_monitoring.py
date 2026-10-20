from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.monitoring import monitoring_snapshot
from app.auth.session import current_user_is_admin
from app.db import Base
from app.models import MailboxConnection, ServiceHeartbeat, User


def _database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_monitoring_snapshot_is_admin_only():
    session_factory = _database()
    with session_factory() as db:
        token = current_user_is_admin.set(False)
        try:
            with pytest.raises(HTTPException) as error:
                monitoring_snapshot(db)
            assert error.value.status_code == 403
        finally:
            current_user_is_admin.reset(token)


def test_monitoring_snapshot_reports_services_and_counts():
    session_factory = _database()
    with session_factory() as db:
        user = User(external_id="monitor-admin", email="admin@example.com", is_admin=True)
        db.add(user)
        db.flush()
        db.add(MailboxConnection(
            user_id=user.id,
            provider="microsoft",
            email_address="mailbox@example.com",
            access_token_enc="x",
            is_active=True,
        ))
        now = datetime.now(timezone.utc)
        for service_name in (
            "email-api",
            "sync-worker",
            "classification-worker",
            "event-relay",
            "notification-service",
        ):
            db.add(ServiceHeartbeat(
                service_name=service_name,
                instance_id=f"test:{service_name}",
                started_at=now,
                last_seen_at=now,
            ))
        db.commit()

        token = current_user_is_admin.set(True)
        try:
            snapshot = monitoring_snapshot(db)
        finally:
            current_user_is_admin.reset(token)

    assert snapshot["overall_status"] == "healthy"
    assert snapshot["metrics"]["users"] == 1
    assert snapshot["metrics"]["active_mailboxes"] == 1
    assert all(service["status"] == "healthy" for service in snapshot["services"])
