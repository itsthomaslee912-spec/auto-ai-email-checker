from datetime import date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import mailboxes
from app.db import Base, get_db
from app.models import EmailLabel, EmailMessage, MailboxConnection, Provider, User


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _client(SessionLocal):
    app = FastAPI()
    app.include_router(mailboxes.router)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _seed_mailbox(db, *, email_address: str) -> MailboxConnection:
    user = db.query(User).filter(User.external_id == "stats-user").one_or_none()
    if user is None:
        user = User(external_id="stats-user")
        db.add(user)
        db.flush()
    mailbox = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address=email_address,
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    db.add(mailbox)
    db.flush()
    return mailbox


def _add_email(db, mailbox_id: int, *, label: str, received_at: datetime, provider_message_id: str) -> None:
    db.add(
        EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id=provider_message_id,
            subject=f"{label} mail",
            sender="recruiter@example.com",
            snippet="snippet",
            body_text="body",
            label=label,
            received_at=received_at,
        )
    )


def test_label_stats_filters_by_date_and_mailbox():
    SessionLocal = _session_factory()
    db = SessionLocal()
    box_a = _seed_mailbox(db, email_address="a@example.com")
    box_b = _seed_mailbox(db, email_address="b@example.com")
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
        received_at=datetime(2026, 8, 10, 12, 0, 0),
        provider_message_id="in-range-interview",
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.OFFER.value,
        received_at=datetime(2026, 8, 20, 9, 0, 0),
        provider_message_id="in-range-offer",
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 1, 2, 9, 0, 0),
        provider_message_id="out-of-range",
    )
    _add_email(
        db,
        box_b.id,
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
        received_at=datetime(2026, 8, 12, 9, 0, 0),
        provider_message_id="other-mailbox",
    )
    db.commit()
    mailbox_id = box_a.id
    db.close()

    client = _client(SessionLocal)
    res = client.get(
        f"/api/mailboxes/{mailbox_id}/label-stats",
        params={"date_from": "2026-08-01", "date_to": "2026-08-31"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mailbox_id"] == mailbox_id
    assert body["date_from"] == "2026-08-01"
    assert body["date_to"] == "2026-08-31"
    assert body["total"] == 2
    counts = body["label_counts"]
    assert counts["interview_scheduled"] == 1
    assert counts["offer"] == 1
    assert counts["rejected_closed"] == 0
    for key in EmailLabel:
        assert key.value in counts


def test_label_stats_rejects_inverted_range_and_missing_mailbox():
    SessionLocal = _session_factory()
    db = SessionLocal()
    box = _seed_mailbox(db, email_address="a@example.com")
    mailbox_id = box.id
    db.commit()
    db.close()

    client = _client(SessionLocal)
    bad_range = client.get(
        f"/api/mailboxes/{mailbox_id}/label-stats",
        params={"date_from": "2026-09-01", "date_to": "2026-08-01"},
    )
    assert bad_range.status_code == 400

    missing = client.get(
        "/api/mailboxes/9999/label-stats",
        params={"date_from": "2026-08-01", "date_to": "2026-08-31"},
    )
    assert missing.status_code == 404


def test_label_stats_hides_inactive_mailbox():
    SessionLocal = _session_factory()
    db = SessionLocal()
    box = _seed_mailbox(db, email_address="gone@example.com")
    box.is_active = False
    mailbox_id = box.id
    db.commit()
    db.close()

    client = _client(SessionLocal)
    res = client.get(
        f"/api/mailboxes/{mailbox_id}/label-stats",
        params={"date_from": date(2026, 8, 1).isoformat(), "date_to": "2026-08-31"},
    )
    assert res.status_code == 404


def test_label_stats_all_accounts_and_exact_datetime_bound():
    SessionLocal = _session_factory()
    db = SessionLocal()
    box_a = _seed_mailbox(db, email_address="a@example.com")
    box_b = _seed_mailbox(db, email_address="b@example.com")
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
        received_at=datetime(2026, 8, 10, 15, 0, 0),
        provider_message_id="a-1500",
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
        received_at=datetime(2026, 8, 10, 16, 0, 0),
        provider_message_id="a-1600",
    )
    _add_email(
        db,
        box_b.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 8, 10, 15, 15, 0),
        provider_message_id="b-1515",
    )
    db.commit()
    mailbox_id = box_a.id
    db.close()

    client = _client(SessionLocal)
    bounded = client.get(
        f"/api/mailboxes/{mailbox_id}/label-stats",
        params={"date_from": "2026-08-10T00:00:00", "date_to": "2026-08-10T15:30:00"},
    )
    assert bounded.status_code == 200, bounded.text
    assert bounded.json()["total"] == 1
    assert bounded.json()["label_counts"]["interview_scheduled"] == 1

    everyone = client.get(
        "/api/mailboxes/label-stats",
        params={"date_from": "2026-08-10", "date_to": "2026-08-10"},
    )
    assert everyone.status_code == 200, everyone.text
    body = everyone.json()
    assert body["mailbox_id"] is None
    assert body["total"] == 3
    assert body["label_counts"]["interview_scheduled"] == 2
    assert body["label_counts"]["rejected_closed"] == 1


def test_label_timeline_uses_hour_or_day_buckets():
    SessionLocal = _session_factory()
    db = SessionLocal()
    box = _seed_mailbox(db, email_address="a@example.com")
    _add_email(
        db,
        box.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 8, 10, 12, 20, 0),
        provider_message_id="same-day",
    )
    _add_email(
        db,
        box.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 8, 2, 9, 0, 0),
        provider_message_id="day-one",
    )
    _add_email(
        db,
        box.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 8, 4, 9, 0, 0),
        provider_message_id="day-three",
    )
    db.commit()
    mailbox_id = box.id
    db.close()

    client = _client(SessionLocal)
    hours = client.get(
        f"/api/mailboxes/{mailbox_id}/label-timeline",
        params={"label": "rejected_closed", "date_from": "2026-08-10", "date_to": "2026-08-10"},
    )
    assert hours.status_code == 200, hours.text
    hour_body = hours.json()
    assert hour_body["bucket"] == "hour"
    hour_counts = {item["bucket"]: item["count"] for item in hour_body["buckets"]}
    assert hour_counts["2026-08-10T12:00:00"] == 1
    assert sum(item["count"] for item in hour_body["buckets"]) == 1

    days = client.get(
        f"/api/mailboxes/{mailbox_id}/label-timeline",
        params={"label": "rejected_closed", "date_from": "2026-08-01", "date_to": "2026-08-05"},
    )
    assert days.status_code == 200, days.text
    day_body = days.json()
    assert day_body["bucket"] == "day"
    day_counts = {item["bucket"]: item["count"] for item in day_body["buckets"]}
    assert day_counts["2026-08-02T00:00:00"] == 1
    assert day_counts["2026-08-04T00:00:00"] == 1
    assert day_counts["2026-08-03T00:00:00"] == 0
