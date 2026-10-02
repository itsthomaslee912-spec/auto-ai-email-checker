from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import calendar_events, emails
from app.db import Base, get_db
from app.models import CalendarEvent, EmailLabel, EmailMessage, MailboxConnection, Provider, User
from app.services.calendar_extract import InterviewDetails, MeetingAccess, _parse_ai_slot, extract_interviewer, extract_meeting_access, parse_ics_slots, parse_text_slots, process_calendar_email, refresh_calendar_event_details


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


def test_start_only_ics_defaults_to_thirty_minutes():
    slots = parse_ics_slots("""BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261005T140000Z
END:VEVENT
END:VCALENDAR""")

    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        (
            "When: Sep 24, 2026, 12:00 PM - 1:00 PM (America/New_York)",
            datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc),
        ),
        (
            "Interview 29 Sep 2026 | 2:00 PM - 2:15 PM America/New_York (EDT)",
            datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 29, 18, 15, tzinfo=timezone.utc),
        ),
        (
            "Thursday Oct 1, 2026 ⋅ 8:30am – 8:45am Central Time - Chicago",
            datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 10, 1, 13, 45, tzinfo=timezone.utc),
        ),
        (
            "DATE/TIME 2026-09-28 01:00 pm - 01:30 pm EDT",
            datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 28, 17, 30, tzinfo=timezone.utc),
        ),
    ],
)
def test_human_readable_interview_times_are_parsed_without_ai(text, start, end):
    slots = parse_text_slots(text, datetime(2026, 9, 20, tzinfo=timezone.utc))

    assert len(slots) == 1
    assert slots[0].start_at == start
    assert slots[0].end_at == end


def test_human_readable_time_without_timezone_is_not_guessed():
    assert parse_text_slots("Interview Oct 1, 2026 at 10:30am") == []


@pytest.mark.parametrize("end", ["", "not-a-date", "2026-10-05T13:00:00Z"])
def test_start_only_or_invalid_ai_end_defaults_to_thirty_minutes(end):
    slot = _parse_ai_slot({"start": "2026-10-05T14:00:00Z", "end": end, "timezone": "UTC"})

    assert slot is not None
    assert slot.start_at == datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    assert slot.end_at == datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)


def test_microsoft_booking_ics_uses_windows_timezone_and_organizer():
    text = """Jordan Meeker - Opportunity with Castelion - Phone Conversation
BEGIN:VCALENDAR
BEGIN:VEVENT
ORGANIZER;CN=Julee Yoo:mailto:julee@castelion.com
DTSTART;TZID=Eastern Standard Time:20261002T133000
DTEND;TZID=Eastern Standard Time:20261002T140000
END:VEVENT
END:VCALENDAR"""
    slots = parse_ics_slots(text)
    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 2, 17, 30, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)
    assert extract_interviewer(text) == "Julee Yoo"
    assert extract_meeting_access(text).meeting_type == "phone"


def test_interviewer_falls_back_to_human_sender_but_not_notification_sender():
    assert extract_interviewer("Phone conversation", "Julee Yoo <julee@castelion.com>") == "Julee Yoo"
    assert extract_interviewer("Interview reminder", "Google Calendar <calendar-notification@google.com>") == ""


def test_existing_available_and_scheduled_events_are_enriched_without_ai():
    factory, (mailbox_id, _) = _database()
    body = """BEGIN:VCALENDAR
BEGIN:VEVENT
ORGANIZER;CN=Julee Yoo:mailto:julee@example.com
DTSTART:20261002T173000Z
DTEND:20261002T180000Z
END:VEVENT
END:VCALENDAR"""
    with factory() as db:
        source = EmailMessage(mailbox_id=mailbox_id, provider_message_id="detail-source", thread_id="detail-thread", subject="Phone conversation", sender="Julee Yoo <julee@example.com>", body_text=body, label=EmailLabel.INTERVIEW_SCHEDULED.value, company="Castelion")
        db.add(source)
        db.flush()
        available = CalendarEvent(mailbox_id=mailbox_id, source_email_id=source.id, kind="availability", origin="email", title="Available interview", start_at=datetime(2026, 10, 2, 17, 30, tzinfo=timezone.utc), end_at=datetime(2026, 10, 2, 18, tzinfo=timezone.utc), meeting_type="unspecified")
        scheduled = CalendarEvent(mailbox_id=mailbox_id, source_email_id=source.id, slot_index=1, kind="confirmed", origin="email", title="Scheduled interview", start_at=datetime(2026, 10, 2, 17, 30, tzinfo=timezone.utc), end_at=datetime(2026, 10, 2, 18, tzinfo=timezone.utc), meeting_type="unspecified")
        db.add_all([available, scheduled])
        db.flush()

        assert refresh_calendar_event_details(db, source) is True
        assert available.interviewer == "Julee Yoo"
        assert scheduled.interviewer == "Julee Yoo"
        assert available.company == "Castelion"
        assert scheduled.company == "Castelion"


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


def test_cancellation_hides_the_existing_confirmed_event():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(mailbox_id=mailbox_id, provider_message_id="cancel-source", thread_id="cancel-thread", subject="Canceled interview", sender="recruiter@example.com", body_text="Canceled", label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="cancellation", company="Acme")
        db.add(email)
        db.flush()
        event = CalendarEvent(mailbox_id=mailbox_id, source_email_id=email.id, source_thread_id=email.thread_id, kind="confirmed", origin="email", title="Acme", start_at=datetime(2026, 10, 5, 14, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc), is_visible=True)
        db.add(event)
        db.flush()

        assert asyncio.run(process_calendar_email(db, email, force=True)) is True
        assert event.interview_status == "cancellation"
        assert event.is_visible is False


def test_separate_thread_confirmation_matches_time_not_another_company_interview():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        earlier = CalendarEvent(mailbox_id=mailbox_id, source_thread_id="old-thread", kind="confirmed", origin="email", title="Anomali first call", company="Anomali", start_at=datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc), end_at=datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc), is_visible=True)
        later = CalendarEvent(mailbox_id=mailbox_id, source_thread_id="other-thread", kind="confirmed", origin="email", title="Anomali second call", company="Anomali", start_at=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc), end_at=datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc), is_visible=True)
        db.add_all([earlier, later])
        db.flush()
        email = EmailMessage(mailbox_id=mailbox_id, provider_message_id="anomali-confirm", thread_id="new-thread", subject="Interview with Anomali", sender="recruiter@example.com", received_at=datetime(2026, 9, 28, tzinfo=timezone.utc), body_text="Confirmed Wednesday, September 30, 2026 at 830am EDT", label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="confirmation", company="Anomali")
        db.add(email)
        db.flush()

        assert asyncio.run(process_calendar_email(db, email)) is True
        assert earlier.source_email_id == email.id
        assert earlier.start_at == datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc)
        assert later.source_email_id is None
        assert later.start_at == datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)


def test_corrected_scheduled_label_reuses_stored_availability_when_extraction_has_no_slot(monkeypatch):
    factory, (mailbox_id, _) = _database()

    async def extract_without_slot(email, related_emails=None):
        return [], MeetingAccess("video", "Microsoft Teams"), InterviewDetails(company="Thomson Reuters"), False

    monkeypatch.setattr("app.services.calendar_extract.extract_calendar_details", extract_without_slot)
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="thomson-confirmed",
            thread_id="thomson-thread",
            subject="Thomson Reuters Interview Invitation",
            sender="recruiter@thomsonreuters.com",
            body_text="Upcoming discussion: Tuesday at 3:00 PM",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Thomson Reuters",
        )
        db.add(email)
        db.flush()
        available = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=email.id,
            source_thread_id=email.thread_id,
            kind="availability",
            origin="email",
            slot_index=0,
            title="Thomson Reuters",
            start_at=datetime(2026, 9, 29, 19, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 29, 20, tzinfo=timezone.utc),
            is_visible=True,
        )
        db.add(available)
        db.flush()
        event_id = available.id

        assert asyncio.run(process_calendar_email(db, email, force=True)) is True
        db.flush()

        corrected = db.get(CalendarEvent, event_id)
        assert corrected.kind == "confirmed"
        assert corrected.interview_status == "confirmation"
        assert corrected.is_visible is True
        assert corrected.start_at == datetime(2026, 9, 29, 19, tzinfo=timezone.utc)


@pytest.mark.parametrize("subtype", ["reminder", "reschedule", "time_change"])
def test_scheduled_update_with_explicit_time_creates_event_when_thread_has_no_match(subtype):
    factory, (mailbox_id, _) = _database()
    body = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261005T160000Z
DTEND:20261005T163000Z
END:VEVENT
END:VCALENDAR"""
    with factory() as db:
        email = EmailMessage(mailbox_id=mailbox_id, provider_message_id=f"scheduled-{subtype}", thread_id=f"thread-{subtype}", subject=f"Interview {subtype}", sender="Julee Yoo <julee@example.com>", body_text=body, label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype=subtype, company="Castelion")
        db.add(email)
        db.flush()
        assert asyncio.run(process_calendar_email(db, email)) is True
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").one()
        assert event.interview_status == subtype
        assert event.interviewer == "Julee Yoo"


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


def test_email_conversation_returns_thread_messages_in_order_without_marking_read():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        first = EmailMessage(mailbox_id=mailbox_id, provider_message_id="thread-first", thread_id="thread-preview", subject="Availability", sender="Julee Yoo <julee@example.com>", received_at=datetime(2026, 10, 1, 14, tzinfo=timezone.utc), body_text="Choose a time", label=EmailLabel.INTERVIEW_INVITATION.value, is_read=False)
        second = EmailMessage(mailbox_id=mailbox_id, provider_message_id="thread-second", thread_id="thread-preview", subject="Interview confirmed", sender="Julee Yoo <julee@example.com>", received_at=datetime(2026, 10, 2, 14, tzinfo=timezone.utc), body_text="Confirmed", label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="confirmation", is_read=False)
        db.add_all([second, first])
        db.commit()
        source_id = second.id
        ids = [first.id, second.id]

    response = _client(factory).get(f"/api/emails/{source_id}/conversation")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ids
    with factory() as db:
        assert all(db.get(EmailMessage, item_id).is_read is False for item_id in ids)


def test_calendar_event_emails_include_source_thread_and_outcome_newest_first():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        invitation = EmailMessage(mailbox_id=mailbox_id, provider_message_id="event-invite", thread_id="event-thread", subject="Choose an interview time", sender="recruiter@example.com", received_at=datetime(2026, 10, 1, 14, tzinfo=timezone.utc), body_text="Invitation", label=EmailLabel.INTERVIEW_INVITATION.value)
        confirmation = EmailMessage(mailbox_id=mailbox_id, provider_message_id="event-confirm", thread_id="event-thread", subject="Interview confirmed", sender="recruiter@example.com", received_at=datetime(2026, 10, 2, 14, tzinfo=timezone.utc), body_text="Confirmation", label=EmailLabel.INTERVIEW_SCHEDULED.value)
        outcome = EmailMessage(mailbox_id=mailbox_id, provider_message_id="event-outcome", thread_id="outcome-thread", subject="Application update", sender="recruiter@example.com", received_at=datetime(2026, 10, 3, 14, tzinfo=timezone.utc), body_text="Outcome", label=EmailLabel.REJECTED_CLOSED.value)
        unrelated = EmailMessage(mailbox_id=mailbox_id, provider_message_id="event-unrelated", thread_id="other-thread", subject="Unrelated", sender="other@example.com", received_at=datetime(2026, 10, 4, 14, tzinfo=timezone.utc), body_text="Other", label=EmailLabel.OTHER.value)
        db.add_all([invitation, confirmation, outcome, unrelated])
        db.flush()
        event = CalendarEvent(mailbox_id=mailbox_id, source_email_id=confirmation.id, outcome_source_email_id=outcome.id, kind="confirmed", origin="email", title="Interview", start_at=datetime(2026, 10, 5, 14, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 15, tzinfo=timezone.utc), is_visible=True)
        db.add(event)
        db.commit()
        event_id = event.id
        expected_ids = [outcome.id, confirmation.id, invitation.id]

    response = _client(factory).get(f"/api/calendar-events/{event_id}/emails")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == expected_ids
    assert all("body_text" in item for item in response.json())


def test_calendar_event_emails_keep_invitation_confirmation_and_updates_from_separate_threads():
    factory, (mailbox_id, _) = _database()
    appointment = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261005T140000Z
DTEND:20261005T143000Z
END:VEVENT
END:VCALENDAR"""
    with factory() as db:
        invitation = EmailMessage(mailbox_id=mailbox_id, provider_message_id="separate-invite", thread_id="invite-thread", subject="Choose an interview time", sender="recruiter@example.com", received_at=datetime(2026, 10, 1, 14, tzinfo=timezone.utc), body_text=appointment, label=EmailLabel.INTERVIEW_INVITATION.value, company="Acme", job_role="Engineer")
        confirmation = EmailMessage(mailbox_id=mailbox_id, provider_message_id="separate-confirm", thread_id="confirm-thread", subject="Interview confirmed", sender="scheduler@example.com", received_at=datetime(2026, 10, 2, 14, tzinfo=timezone.utc), body_text=appointment, label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="confirmation", company="Acme", job_role="Engineer")
        reminder = EmailMessage(mailbox_id=mailbox_id, provider_message_id="separate-reminder", thread_id="reminder-thread", subject="Interview reminder", sender="scheduler@example.com", received_at=datetime(2026, 10, 3, 14, tzinfo=timezone.utc), body_text=appointment, label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="reminder", company="Acme", job_role="Engineer")
        db.add_all([invitation, confirmation, reminder])
        db.flush()

        assert asyncio.run(process_calendar_email(db, invitation, use_ai=False)) is True
        assert asyncio.run(process_calendar_email(db, confirmation, use_ai=False)) is True
        assert asyncio.run(process_calendar_email(db, reminder, use_ai=False)) is True
        db.commit()
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").one()
        event_id = event.id
        expected_ids = [reminder.id, confirmation.id, invitation.id]

    response = _client(factory).get(f"/api/calendar-events/{event_id}/emails")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == expected_ids


def test_manual_event_create_and_update_validates_times():
    factory, (mailbox_id, _) = _database()
    client = _client(factory)
    payload = {"mailbox_id": mailbox_id, "title": "Manual interview", "interviewer": "Jamie Chen", "work_model": "hybrid", "job_url": "https://example.com/jobs/123", "start_at": "2026-10-05T14:00:00Z", "end_at": "2026-10-05T15:00:00Z", "meeting_type": "video", "meeting_url": "https://zoom.us/j/123"}
    created = client.post("/api/calendar-events", json=payload)
    assert created.status_code == 201
    assert created.json()["origin"] == "manual"
    assert created.json()["interviewer"] == "Jamie Chen"
    assert created.json()["work_model"] == "hybrid"
    assert created.json()["job_url"] == "https://example.com/jobs/123"
    updated = client.patch(f"/api/calendar-events/{created.json()['id']}", json={"title": "Updated interview", "interviewer": "Morgan Lee", "work_model": "remote", "application_status": "position_closed"})
    assert updated.status_code == 200
    assert updated.json()["title"] == "Updated interview"
    assert updated.json()["interviewer"] == "Morgan Lee"
    assert updated.json()["work_model"] == "remote"
    assert updated.json()["application_status"] == "position_closed"
    invalid = client.post("/api/calendar-events", json={**payload, "end_at": payload["start_at"]})
    assert invalid.status_code == 422


def test_selected_ai_extracts_full_calendar_details_from_related_email_thread(monkeypatch):
    factory, (mailbox_id, _) = _database()
    captured: dict = {}

    async def create(**kwargs):
        captured.update(kwargs)
        content = json.dumps({
            "slots": [],
            "company": "Acme Labs",
            "role": "Platform Engineer",
            "interviewer": "Jamie Chen",
            "work_model": "hybrid",
            "job_url": "https://acme.test/careers/platform-engineer",
            "meeting_type": "video",
            "meeting_provider": "Google Meet",
            "meeting_url": "https://meet.google.com/abc-defg-hij",
            "phone_number": "",
            "access_code": "",
        })
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    backend = SimpleNamespace(api_key="configured", model="selected-calendar-model", client=lambda: client)
    monkeypatch.setattr("app.services.calendar_extract.get_ai_backend", lambda: backend)

    with factory() as db:
        related = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="related-job",
            thread_id="thread-ai",
            subject="Platform Engineer at Acme Labs",
            sender="recruiter@acme.test",
            body_text="The role is hybrid. Job: https://acme.test/careers/platform-engineer",
            label=EmailLabel.SCREENING.value,
        )
        db.add(related)
        db.flush()
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="interview-invite",
            thread_id="thread-ai",
            subject="Interview availability",
            sender="recruiter@acme.test",
            body_text="""BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261008T150000Z
DTEND:20261008T153000Z
END:VEVENT
END:VCALENDAR""",
            label=EmailLabel.INTERVIEW_INVITATION.value,
        )
        db.add(invitation)
        db.flush()

        assert asyncio.run(process_calendar_email(db, invitation)) is True
        db.flush()
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "availability").one()
        assert event.company == "Acme Labs"
        assert event.job_role == "Platform Engineer"
        assert event.interviewer == "Jamie Chen"
        assert event.work_model == "hybrid"
        assert event.job_url == "https://acme.test/careers/platform-engineer"
        assert event.meeting_type == "video"
        assert event.meeting_provider == "Google Meet"
        assert event.meeting_url == "https://meet.google.com/abc-defg-hij"

    assert captured["model"] == "selected-calendar-model"
    prompt = captured["messages"][1]["content"]
    assert "Platform Engineer at Acme Labs" in prompt
    assert "Interview availability" in prompt
