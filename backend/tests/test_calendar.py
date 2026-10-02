from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import calendar_events, emails
from app.db import Base, get_db
from app.models import CalendarEvent, EmailLabel, EmailMessage, MailboxConnection, Provider, User
from app.services.calendar_extract import extract_meeting_access, parse_ics_slots, process_calendar_email


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with factory() as db:
        user = User(external_id="calendar-user")
        db.add(user)
        db.flush()
        boxes = [
            MailboxConnection(user_id=user.id, provider=Provider.GOOGLE.value, email_address="first@example.com", access_token_enc="x", refresh_token_enc="", is_active=True),
            MailboxConnection(user_id=user.id, provider=Provider.MICROSOFT.value, email_address="second@example.com", access_token_enc="x", refresh_token_enc="", is_active=True),
        ]
        db.add_all(boxes)
        db.commit()
        ids = [box.id for box in boxes]
    return factory, ids


def _client(factory):
    app = FastAPI()
    app.include_router(calendar_events.router)
    app.include_router(emails.router)

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_ics_and_meeting_access_are_parsed_without_ai():
    text = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20261005T100000
DTEND;TZID=America/New_York:20261005T103000
END:VEVENT
END:VCALENDAR
Join https://meet.google.com/abc-defg-hij or call +1 (212) 555-0199, PIN: 456 789
"""
    slots = parse_ics_slots(text)
    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)
    access = extract_meeting_access(text)
    assert access.meeting_type == "video"
    assert access.provider == "Google Meet"
    assert access.url == "https://meet.google.com/abc-defg-hij"
    assert access.phone_number == "+1 (212) 555-0199"
    assert access.access_code == "456 789"


def test_confirmation_hides_availability_and_rejection_updates_scheduled_event():
    factory, (mailbox_id, _) = _database()
    availability_ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261005T140000Z
DTEND:20261005T143000Z
END:VEVENT
BEGIN:VEVENT
DTSTART:20261005T160000Z
DTEND:20261005T163000Z
END:VEVENT
END:VCALENDAR"""
    confirmed_ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261005T160000Z
DTEND:20261005T163000Z
END:VEVENT
END:VCALENDAR
Join https://zoom.us/j/123456789"""
    with factory() as db:
        invitation = EmailMessage(mailbox_id=mailbox_id, provider_message_id="invite", thread_id="thread-1", subject="Choose an interview time", sender="recruiter@acme.test", body_text=availability_ics, label=EmailLabel.INTERVIEW_INVITATION.value, company="Acme", job_role="Engineer")
        db.add(invitation)
        db.flush()
        assert asyncio.run(process_calendar_email(db, invitation)) is True
        db.commit()
        assert db.query(CalendarEvent).filter(CalendarEvent.kind == "availability", CalendarEvent.is_visible.is_(True)).count() == 2

        confirmation = EmailMessage(mailbox_id=mailbox_id, provider_message_id="confirm", thread_id="thread-1", subject="Interview confirmed", sender="recruiter@acme.test", body_text=confirmed_ics, label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="confirmation", company="Acme", job_role="Engineer")
        db.add(confirmation)
        db.flush()
        assert asyncio.run(process_calendar_email(db, confirmation)) is True
        db.commit()
        assert db.query(CalendarEvent).filter(CalendarEvent.kind == "availability", CalendarEvent.is_visible.is_(True)).count() == 0
        scheduled = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").one()
        assert scheduled.meeting_provider == "Zoom"

        rejection = EmailMessage(mailbox_id=mailbox_id, provider_message_id="reject", thread_id="new-thread", subject="Update on your application", sender="recruiter@acme.test", body_text="We decided not to move forward.", label=EmailLabel.REJECTED_CLOSED.value, company="Acme", job_role="Engineer")
        db.add(rejection)
        db.flush()
        assert asyncio.run(process_calendar_email(db, rejection)) is True
        db.commit()
        assert db.get(CalendarEvent, scheduled.id).application_status == "rejected"


def test_calendar_api_loads_all_accounts_by_range_and_email_preview_can_stay_unread():
    factory, mailbox_ids = _database()
    with factory() as db:
        source = EmailMessage(mailbox_id=mailbox_ids[0], provider_message_id="source", subject="Interview", sender="r@example.com", snippet="Details", body_text="Interview details", label=EmailLabel.INTERVIEW_SCHEDULED.value, is_read=False)
        db.add(source)
        db.flush()
        for index, mailbox_id in enumerate(mailbox_ids):
            db.add(CalendarEvent(mailbox_id=mailbox_id, source_email_id=source.id if index == 0 else None, kind="confirmed", origin="email", title=f"Interview {index}", start_at=datetime(2026, 10, 5, 14 + index, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 15 + index, tzinfo=timezone.utc), is_visible=True, interview_status="confirmation"))
        db.add(CalendarEvent(mailbox_id=mailbox_ids[0], kind="availability", origin="email", title="Hidden", start_at=datetime(2026, 10, 5, 12, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 13, tzinfo=timezone.utc), is_visible=False))
        db.commit()
        source_id = source.id

    client = _client(factory)
    response = client.get("/api/calendar-events", params={"start": "2026-10-05T00:00:00Z", "end": "2026-10-06T00:00:00Z"})
    assert response.status_code == 200
    assert {row["email_account"] for row in response.json()} == {"first@example.com", "second@example.com"}
    assert len(response.json()) == 2

    preview = client.get(f"/api/emails/{source_id}", params={"mark_read": "false"})
    assert preview.status_code == 200
    with factory() as db:
        assert db.get(EmailMessage, source_id).is_read is False


def test_manual_event_create_and_update_validates_times():
    factory, (mailbox_id, _) = _database()
    client = _client(factory)
    payload = {"mailbox_id": mailbox_id, "title": "Manual interview", "start_at": "2026-10-05T14:00:00Z", "end_at": "2026-10-05T15:00:00Z", "meeting_type": "video", "meeting_url": "https://zoom.us/j/123"}
    created = client.post("/api/calendar-events", json=payload)
    assert created.status_code == 201
    assert created.json()["origin"] == "manual"
    updated = client.patch(f"/api/calendar-events/{created.json()['id']}", json={"title": "Updated interview", "application_status": "position_closed"})
    assert updated.status_code == 200
    assert updated.json()["title"] == "Updated interview"
    assert updated.json()["application_status"] == "position_closed"
    invalid = client.post("/api/calendar-events", json={**payload, "end_at": payload["start_at"]})
    assert invalid.status_code == 422
