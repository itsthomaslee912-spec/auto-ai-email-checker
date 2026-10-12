from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import calendar_events, emails
from app.db import Base, get_db
from app.models import CalendarEvent, CalendarEventEmail, EmailLabel, EmailMessage, MailboxConnection, Provider, User
from app.services.calendar_extract import InterviewDetails, MeetingAccess, _parse_ai_slot, _resolve_video_landing_page, _without_quoted_history, clean_company_name, collapse_duplicate_calendar_events, companies_match, employer_instead_of_sender_domain, explicit_hiring_employer, extract_calendar_details, extract_interviewer, extract_meeting_access, fuller_company_name, hide_open_reschedules, is_interview_stage_label, job_role_or_empty, leaves_interview_unbooked, message_appointment_slots, message_is_booked_appointment, needs_calendar_event_repair, parse_ics_slots, parse_text_slots, process_calendar_email, refresh_calendar_event_details, repair_availability_window_appointments, repair_interview_stage_roles, repair_meeting_join_urls, repair_meeting_landing_urls, repair_phantom_availability_slots, repair_quoted_option_bookings


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


def test_named_video_provider_is_preserved_without_a_direct_join_url():
    access = extract_meeting_access(
        "Your meeting will be a video call on Google Meet. Click below to join."
    )

    assert access == MeetingAccess("video", "Google Meet")


def test_government_microsoft_teams_join_link_is_recognized():
    url = (
        "https://gov.teams.microsoft.us/l/meetup-join/"
        "19%3agcch%3ameeting_9a19e26025d14a18849ea9108d09ca21%40thread.v2/0"
        "?context=%7b%22Tid%22%3a%2277443add-4970-475d-8910-7855da11b574%22%7d"
    )

    access = extract_meeting_access(f"Microsoft Teams meeting\nJoin: {url}\\nMeeting ID: 992518179706")

    assert access == MeetingAccess("video", "Microsoft Teams", url)


def test_zoom_branding_image_is_not_used_as_the_join_link():
    from app.services.calendar_extract import is_video_join_url

    text = """DESCRIPTION;LANGUAGE=en-US:<img src="https://zoom.us/account/branding/p/5946e0bd.png">
Rachel Tuenge is inviting you to a scheduled Zoom meeting.
Join Zoom Meeting
https://fanatics.zoom.us/j/91763586848?pwd=uPKCWZIARXwGitIxdu4dw3u8VGiu8N.1
Meeting ID: 917 6358 6848
"""
    assert not is_video_join_url("https://zoom.us/account/branding/p/594")
    assert is_video_join_url("https://fanatics.zoom.us/j/91763586848?pwd=uPKCWZIARXwGitIxdu4dw3u8VGiu8N.1")
    assert is_video_join_url(
        "https://app.zoom.us/wc/91763586848/join?pwd=uPKCWZIARXwGitIxdu4dw3u8VGiu8N.1&fromPWA=1"
    )
    access = extract_meeting_access(text)
    assert access.provider == "Zoom"
    assert access.url == "https://fanatics.zoom.us/j/91763586848?pwd=uPKCWZIARXwGitIxdu4dw3u8VGiu8N.1"


def test_repair_replaces_zoom_branding_url_with_join_link():
    from app.services.calendar_extract import repair_meeting_join_urls

    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="fanatics-zoom",
            thread_id="fanatics-zoom",
            subject="Interview with Fanatics Commerce",
            sender="Rachel <rachel@fanatics.com>",
            received_at=datetime(2026, 10, 5, 17, tzinfo=timezone.utc),
            body_text="Join Zoom Meeting\nhttps://fanatics.zoom.us/j/91763586848?pwd=uPKCWZIARXwGitIxdu4dw3u8VGiu8N.1\n",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            company="Fanatics Commerce",
            job_role="Staff Software Engineer - AI/ML Platform",
        )
        db.add(email)
        db.flush()
        event = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=email.id,
            kind="confirmed",
            origin="email",
            company="Fanatics Commerce",
            job_role="Staff Software Engineer - AI/ML Platform",
            start_at=datetime(2026, 10, 5, 17, 15, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 5, 17, 45, tzinfo=timezone.utc),
            meeting_type="unspecified",
            meeting_provider="Zoom",
            meeting_url="https://zoom.us/account/branding/p/594",
            is_visible=True,
        )
        db.add(event)
        db.commit()
        assert repair_meeting_join_urls(db) >= 1
        db.commit()
        assert event.meeting_url == "https://fanatics.zoom.us/j/91763586848?pwd=uPKCWZIARXwGitIxdu4dw3u8VGiu8N.1"


def test_repair_uses_a_linked_invite_for_the_same_appointment():
    factory, (mailbox_id, _) = _database()
    start = datetime(2026, 10, 13, 16, tzinfo=timezone.utc)
    join_url = (
        "https://gov.teams.microsoft.us/l/meetup-join/"
        "19%3ameeting_9a19e26025d14a18849ea9108d09ca21%40thread.v2/0"
    )
    with factory() as db:
        reply = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="rivet-reply",
            subject="RE: Next Steps with Rivet Industries",
            sender="recruiter@rivet.us",
            received_at=datetime(2026, 10, 5, 17, 29, tzinfo=timezone.utc),
            body_text="Your interview was rescheduled. I sent an updated invite.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="rivet-invite",
            subject="Interview with Rivet Industries",
            sender="recruiter@rivet.us",
            received_at=datetime(2026, 10, 5, 17, 27, tzinfo=timezone.utc),
            body_text=(
                f"Join: {join_url}\nBEGIN:VCALENDAR\nBEGIN:VEVENT\n"
                "DTSTART:20261013T160000Z\nDTEND:20261013T161500Z\n"
                "END:VEVENT\nEND:VCALENDAR"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
        )
        db.add_all([reply, invite])
        db.flush()
        event = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=reply.id,
            kind="confirmed",
            origin="email",
            title="Rivet Industries — ML Researcher",
            company="Rivet Industries",
            job_role="ML Researcher",
            start_at=start,
            end_at=start + timedelta(minutes=15),
            meeting_type="video",
            meeting_provider="Microsoft Teams",
            is_visible=True,
        )
        db.add(event)
        db.flush()
        db.add_all([
            CalendarEventEmail(event_id=event.id, email_id=reply.id),
            CalendarEventEmail(event_id=event.id, email_id=invite.id),
        ])
        db.commit()

        assert repair_meeting_join_urls(db, mailbox_id) == 1
        assert event.meeting_url == join_url
        collapse_duplicate_calendar_events(db, mailbox_id)
        assert event.meeting_url == join_url


def test_teamtailor_landing_page_resolves_provider_join_url(monkeypatch):
    schedule_url = "https://careers.example.com/schedule/5d6de20f-e95c-443c-881a-ffaec8340694"
    gateway_url = "https://careers.example.com/video_meetings/73a303c3-72d6-4209-bd2a-374ea819f5fd"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == schedule_url:
            return httpx.Response(
                200,
                text=f'<html>Powered by Teamtailor <a href="{gateway_url}">Google Meet</a></html>',
            )
        if str(request.url) == gateway_url:
            return httpx.Response(302, headers={"location": "https://meet.google.com/abc-defg-hij"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.calendar_extract.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    access = asyncio.run(
        _resolve_video_landing_page(f"Recruiting powered by Teamtailor\n{schedule_url}")
    )

    assert access == MeetingAccess(
        "video", "Google Meet", "https://meet.google.com/abc-defg-hij"
    )


def test_paradox_join_link_resolves_to_provider_url(monkeypatch):
    landing_url = "https://olivia.paradox.ai/Tu4qc3Vh"
    teams_url = (
        "https://teams.microsoft.com/l/meetup-join/"
        "19%3ameeting_MDY0NTZmN2EtNWY2NC00MTI3LWI3ZjMtODQ5OWY5OTBhMjBi%40thread.v2/0"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == landing_url:
            return httpx.Response(302, headers={"location": teams_url})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.calendar_extract.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    access = asyncio.run(
        _resolve_video_landing_page(
            f"Click here to join the interview by Microsoft Teams: {landing_url}"
        )
    )

    assert access == MeetingAccess("video", "Microsoft Teams", teams_url)


def test_paradox_short_domain_resolves_through_allow_listed_host(monkeypatch):
    short_url = "https://oli.vi/zuAeempA"
    landing_url = "https://olivia.paradox.ai/zuAeempA"
    teams_url = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abcdef123456%40thread.v2/0"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == short_url:
            return httpx.Response(308, headers={"location": landing_url})
        if str(request.url) == landing_url:
            return httpx.Response(302, headers={"location": teams_url})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.calendar_extract.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    access = asyncio.run(_resolve_video_landing_page(f"Join Microsoft Teams: {short_url}"))

    assert access == MeetingAccess("video", "Microsoft Teams", teams_url)


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://record.interview.video/7679465494990026.pPAapvUwLTthGoCb8uRkH9", "Recorded Video Interview"),
        ("https://meet.alex.com/mitchellmartin-29142320?sessionId=abc", "Alex Video Interview"),
        ("https://example.pinpointhq.com/candidates/interview_parts/abc123/join", "Video Interview"),
    ],
)
def test_direct_interview_platform_links_are_recognized(url, provider):
    assert extract_meeting_access(f"Join your interview: {url}") == MeetingAccess("video", provider, url)


def test_repair_resolves_missing_trusted_landing_page(monkeypatch):
    factory, (mailbox_id, _) = _database()
    landing_url = "https://olivia.paradox.ai/Tu4qc3Vh"
    teams_url = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abcdef123456%40thread.v2/0"

    async def resolve(text: str):
        assert landing_url in text
        return MeetingAccess("video", "Microsoft Teams", teams_url)

    monkeypatch.setattr("app.services.calendar_extract._resolve_video_landing_page", resolve)
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="pfizer-confirmed",
            subject="Pfizer virtual interview Confirmed",
            sender="assistant@pfizer.com",
            body_text=f"Click here to join the interview by Microsoft Teams: {landing_url}",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
        )
        db.add(email)
        db.flush()
        event = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=email.id,
            kind="confirmed",
            origin="email",
            title="Pfizer — Interview",
            company="Pfizer",
            start_at=datetime(2026, 10, 15, 14, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 15, 15, 30, tzinfo=timezone.utc),
            meeting_type="video",
            meeting_provider="Microsoft Teams",
            is_visible=True,
        )
        db.add(event)
        db.commit()

        repaired = asyncio.run(repair_meeting_landing_urls(db, mailbox_id))

        assert repaired == {mailbox_id}
        assert event.meeting_type == "video"
        assert event.meeting_url == teams_url


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


def test_reply_header_time_does_not_replace_the_scheduled_interview():
    text = """On Mon, Sep 28, 2026 at 7:24 PM <jordanwm914@gmail.com> wrote:
Thanks for the quick reply. I sent an invite for 10/1 at 10am PST.
Sep 28, 2026 at 7:24 PM <div dir="ltr">Thanks for the quick reply. I sent an invite for 10/1 at 10am PST."""
    slots = parse_text_slots(text, datetime(2026, 9, 29, 2, 29, tzinfo=timezone.utc))

    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
    assert slots[0].end_is_explicit is False


def test_spark_quote_clock_is_not_read_as_central_time():
    text = """Hi Chris,

Thanks for letting me know. Would today at 9:00 AM PST work for you?

Best,
Mark

Sent with Spark
On Oct 1, 2026, 9:56 AM -0400, Chris Bujoreanu <cbujoreanu@zacharypiper.com>, wrote:
Hi Mark, Unfortunately, I am unavailable at 2pm PST, but am available at 1pm PST or in the morning.
"""
    received = datetime(2026, 10, 1, 13, 59, tzinfo=timezone.utc)
    slots = parse_text_slots(text, received)

    assert datetime(2026, 10, 1, 14, 56, tzinfo=timezone.utc) not in {slot.start_at for slot in slots}
    assert slots[0].start_at == datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
    assert slots[0].source_timezone == "America/Los_Angeles"

    email = SimpleNamespace(
        subject="Re: ZPS - Infrastructure Engineer Application",
        snippet="",
        body_text=text,
        body_html="",
        received_at=received,
    )
    appointment = message_appointment_slots(email)
    assert [slot.start_at for slot in appointment] == [datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)]


def test_october_date_is_not_parsed_as_central_time():
    slots = parse_text_slots(
        "Interview on Oct 1, 2026, 9:56 AM ET",
        datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
    )

    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 1, 13, 56, tzinfo=timezone.utc)
    assert slots[0].source_timezone == "America/New_York"


def test_unknown_sender_invite_uses_the_subject_time_without_a_calendar_file():
    subject = (
        "Invitation from an unknown sender: Mark Wei and Tim Griffiths @ "
        "Tue Sep 29, 2026 9pm - 9:45pm (CDT) (mark.abraham.wei@gmail.com)"
    )
    slots = parse_text_slots(
        f"{subject}\nThis event isn't in your calendar yet.",
        datetime(2026, 9, 25, 12, 14, tzinfo=timezone.utc),
    )

    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 9, 30, 2, 45, tzinfo=timezone.utc)
    assert slots[0].end_is_explicit is True


def test_explicit_subject_time_does_not_wait_for_the_model(monkeypatch):
    def fail_backend():
        raise AssertionError("model should not be called when the time is already explicit")

    monkeypatch.setattr("app.services.calendar_extract.get_ai_backend", fail_backend)
    email = SimpleNamespace(
        subject=(
            "Invitation from an unknown sender: Mark Wei and Tim Griffiths @ "
            "Tue Sep 29, 2026 9pm - 9:45pm (CDT)"
        ),
        snippet="",
        body_text="This event isn't in your calendar yet.",
        body_html="",
        sender="Tim Griffiths <tim.griffiths@mavensecurities.com>",
        received_at=datetime(2026, 9, 25, 12, 14, tzinfo=timezone.utc),
        company="Maven Securities",
        job_role="Phone Screen",
    )

    slots, _access, details, completed = asyncio.run(extract_calendar_details(email, use_ai=True))

    assert completed is True
    assert slots[0].start_at == datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
    assert details.company == "Maven Securities"


def test_unknown_sender_invite_becomes_a_visible_calendar_item():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="maven-unknown-sender",
            thread_id="maven-thread",
            subject=(
                "Invitation from an unknown sender: Mark Wei and Tim Griffiths @ "
                "Tue Sep 29, 2026 9pm - 9:45pm (CDT) (mark.abraham.wei@gmail.com)"
            ),
            sender="Tim Griffiths <tim.griffiths@mavensecurities.com>",
            received_at=datetime(2026, 9, 25, 12, 14, tzinfo=timezone.utc),
            body_text=(
                "This event isn't in your calendar yet. "
                "You haven't interacted with tim.griffiths@mavensecurities.com before.\n"
                "https://mavensecurities.zoom.us/j/93169163898"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Maven Securities",
            job_role="Phone Screen",
        )
        db.add(email)
        db.flush()

        assert asyncio.run(process_calendar_email(db, email, use_ai=False)) is True
        event = db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True)).one()
        assert event.company == "Maven Securities"
        assert event.start_at == datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
        assert event.end_at == datetime(2026, 9, 30, 2, 45, tzinfo=timezone.utc)
        assert event.meeting_url == "https://mavensecurities.zoom.us/j/93169163898"


def test_sent_reply_proposing_a_time_becomes_an_available_calendar_slot():
    """A candidate's own Sent reply naming a time is not an employer booking,

    but it should still appear on the calendar as an offered slot awaiting
    confirmation -- it must not be silently dropped because it lives in Sent.
    """
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="angelica-invite",
            thread_id="cloud-platform-thread",
            folder="inbox",
            subject="Cloud Platform Engineer III Interview",
            sender="Angelica Chacon <angelica.chacon@utah.edu>",
            received_at=datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc),
            body_text="Are you still interested in the Cloud Platform Engineer III role? Let me know your availability.",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="University of Utah",
            job_role="Cloud Platform Engineer III",
        )
        db.add(invitation)
        db.flush()

        reply = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="mark-reply",
            thread_id="cloud-platform-thread",
            folder="sent",
            subject="Re: Cloud Platform Engineer III Interview",
            sender="Mark Abraham Wei <mark.abraham.wei@gmail.com>",
            received_at=datetime(2026, 10, 1, 17, 12, tzinfo=timezone.utc),
            body_text=(
                "Hi Angelica,\n\n"
                "Thank you for reaching out. Yes, I am still very interested in the Cloud Platform "
                "Engineer III role.\n\n"
                "I am available on October 5th at 2:00 PM PST for the 30-minute virtual interview. "
                "Please let me know if that time works for you.\n\n"
                "Best regards,\nMark"
            ),
            label="other",
        )
        db.add(reply)
        db.flush()

        assert asyncio.run(process_calendar_email(db, reply, use_ai=False)) is True
        assert reply.label == EmailLabel.INTERVIEW_AVAILABILITY.value
        event = db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True)).one()
        assert event.kind == "availability"
        assert event.source_email_id == reply.id
        assert event.start_at == datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)


def test_sent_reply_with_two_options_overrides_scheduled_misclassification():
    """A candidate offering alternatives is not booked just because the
    classifier called the sent message an interview confirmation."""
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="swift-invite",
            thread_id="swift-thread",
            folder="inbox",
            subject="RE: FW: Swift: Job Opportunity",
            sender="Richard <richard@swift.com>",
            received_at=datetime(2026, 10, 7, 19, 51, tzinfo=timezone.utc),
            body_text="Can you please provide me with your availability this week?",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Swift",
            job_role="Senior Storage DevOps",
        )
        reply = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="swift-options",
            thread_id="swift-thread",
            folder="sent",
            subject="RE: FW: Swift: Job Opportunity",
            sender="candidate@example.com",
            received_at=datetime(2026, 10, 7, 19, 55, tzinfo=timezone.utc),
            body_text=(
                "I'd be glad to speak with the hiring manager. I'm available "
                "October 8 at 9:00 AM (CDT) or October 9 at 2:00 PM (CDT)."
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Swift",
            job_role="Senior Storage DevOps",
        )
        db.add_all([invitation, reply])
        db.flush()

        assert asyncio.run(process_calendar_email(db, reply, use_ai=False)) is True
        assert reply.label == EmailLabel.INTERVIEW_AVAILABILITY.value
        assert reply.interview_subtype is None
        events = db.query(CalendarEvent).filter(
            CalendarEvent.source_email_id == reply.id,
            CalendarEvent.is_visible.is_(True),
        ).order_by(CalendarEvent.start_at).all()
        assert [event.kind for event in events] == ["availability", "availability"]
        assert [event.start_at for event in events] == [
            datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 9, 19, 0, tzinfo=timezone.utc),
        ]


def test_sent_acceptance_of_one_time_can_remain_scheduled():
    """A clear acceptance of one employer-proposed time is not demoted."""
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="single-time-invite",
            thread_id="single-time-thread",
            folder="inbox",
            subject="Cloud Platform Engineer Interview",
            sender="recruiter@example.com",
            received_at=datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc),
            body_text="Can you meet October 8 at 2:00 PM PST?",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Example Co",
            job_role="Cloud Platform Engineer",
        )
        reply = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="single-time-acceptance",
            thread_id="single-time-thread",
            folder="sent",
            subject="Re: Cloud Platform Engineer Interview",
            sender="candidate@example.com",
            received_at=datetime(2026, 10, 7, 18, 5, tzinfo=timezone.utc),
            body_text="October 8 at 2:00 PM PST works well for me. I look forward to speaking with you.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Example Co",
            job_role="Cloud Platform Engineer",
        )
        db.add_all([invitation, reply])
        db.flush()

        assert asyncio.run(process_calendar_email(db, reply, use_ai=False)) is True
        assert reply.label == EmailLabel.INTERVIEW_SCHEDULED.value
        event = db.query(CalendarEvent).filter(CalendarEvent.source_email_id == reply.id).one()
        assert event.kind == "confirmed"


def test_sent_reply_without_a_shared_thread_id_matches_by_subject():
    """Mail sync does not always carry a thread id onto a Sent copy.

    The invitation and the reply must still be linked by their (reply-stripped)
    subject so the proposed time is not lost, matching a real production case
    where both messages had thread_id=None.
    """
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="angelica-invite-2",
            thread_id=None,
            folder="inbox",
            subject="Cloud Platform Engineer III Interview",
            sender="Angelica Chacon <angelica.chacon@utah.edu>",
            received_at=datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc),
            body_text="Are you still interested in the Cloud Platform Engineer III role?",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="University of Utah",
            job_role="Cloud Platform Engineer III",
        )
        db.add(invitation)
        db.flush()

        reply = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="mark-reply-2",
            thread_id=None,
            folder="sent",
            subject="Re: Cloud Platform Engineer III Interview",
            sender="Mark Abraham Wei <mark.abraham.wei@gmail.com>",
            received_at=datetime(2026, 10, 1, 17, 12, tzinfo=timezone.utc),
            body_text="I am available on October 5th at 2:00 PM PST for the 30-minute virtual interview.",
            label="other",
        )
        db.add(reply)
        db.flush()

        assert asyncio.run(process_calendar_email(db, reply, use_ai=False)) is True
        event = db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True)).one()
        assert event.kind == "availability"
        assert event.source_email_id == reply.id
        assert event.start_at == datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)


def test_sent_reply_without_an_interview_thread_is_ignored():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        reply = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="unrelated-sent",
            thread_id="unrelated-thread",
            folder="sent",
            subject="Re: Interview prep notes",
            sender="Mark Abraham Wei <mark.abraham.wei@gmail.com>",
            received_at=datetime(2026, 10, 1, 17, 12, tzinfo=timezone.utc),
            body_text="I am available on October 5th at 2:00 PM PST, talk then.",
            label="other",
        )
        db.add(reply)
        db.flush()

        assert asyncio.run(process_calendar_email(db, reply, use_ai=False)) is False
        assert db.query(CalendarEvent).count() == 0


def test_date_glued_to_the_start_time_keeps_one_range():
    slots = parse_text_slots(
        "Friday, September 25, 2026:1:30pm - 2:15pm PT - Initial Screen",
        datetime(2026, 9, 23, tzinfo=timezone.utc),
    )

    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 9, 25, 20, 30, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 9, 25, 21, 15, tzinfo=timezone.utc)
    assert slots[0].end_is_explicit is True


def test_end_time_phantom_collapses_into_the_stated_interview():
    factory, (mailbox_id, _) = _database()
    real_start = datetime(2026, 9, 25, 20, 30, tzinfo=timezone.utc)
    real_end = datetime(2026, 9, 25, 21, 15, tzinfo=timezone.utc)
    with factory() as db:
        confirmation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="adyen-confirm",
            thread_id="adyen-confirm",
            subject="Adyen | Interview",
            sender="Andrew DePaola <andrew.depaola@adyen.com>",
            received_at=datetime(2026, 9, 23, 14, 47, tzinfo=timezone.utc),
            body_text="Friday, September 25, 2026:1:30pm - 2:15pm PT - Initial Screen",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Adyen",
            job_role="Staff Engineer - Customer Interfaces",
        )
        availability = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="adyen-ask",
            thread_id="adyen-ask",
            subject="Regarding your application to Adyen",
            sender="Andrew DePaola <andrew.depaola@adyen.com>",
            received_at=datetime(2026, 9, 23, 13, 36, tzinfo=timezone.utc),
            body_text="Are you available this Friday at 1:30pm PST?",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Adyen",
            job_role="",
        )
        db.add_all([confirmation, availability])
        db.flush()
        phantom = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=confirmation.id,
            kind="confirmed",
            origin="email",
            company="Adyen",
            job_role="Staff Engineer - Customer Interfaces",
            start_at=real_end,
            end_at=real_end + timedelta(minutes=30),
            interview_status="confirmation",
            application_status="active",
            source_received_at=confirmation.received_at,
            is_visible=True,
            meeting_url="https://adyen.zoom.us/j/92099198955",
        )
        asked = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=availability.id,
            kind="confirmed",
            origin="email",
            company="Adyen",
            job_role="Staff Engineer - Customer Interfaces",
            start_at=real_start,
            end_at=real_end,
            interview_status="confirmation",
            application_status="active",
            source_received_at=availability.received_at,
            is_visible=True,
            meeting_url="https://adyen.zoom.us/j/92099198955",
        )
        separate = CalendarEvent(
            mailbox_id=mailbox_id,
            kind="confirmed",
            origin="email",
            company="Adyen",
            job_role="Staff Engineer - Customer Interfaces",
            start_at=datetime(2026, 9, 21, 16, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 21, 17, tzinfo=timezone.utc),
            interview_status="confirmation",
            application_status="active",
            source_received_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
            is_visible=True,
        )
        db.add_all([phantom, asked, separate])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        visible = db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True)).order_by(CalendarEvent.start_at).all()
        assert [(event.start_at, event.end_at) for event in visible] == [
            (datetime(2026, 9, 21, 16, tzinfo=timezone.utc), datetime(2026, 9, 21, 17, tzinfo=timezone.utc)),
            (real_start, real_end),
        ]


def test_central_clock_range_is_kept_when_the_zone_follows_the_end():
    slots = parse_text_slots(
        "When: Thu Oct 1, 2026 12pm - 12:45pm (CDT)",
        datetime(2026, 9, 29, tzinfo=timezone.utc),
    )

    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 1, 17, 45, tzinfo=timezone.utc)
    assert slots[0].end_is_explicit is True


def test_day_first_date_without_year_and_stated_duration_use_received_year():
    slots = parse_text_slots(
        """Date: Tuesday, 29th September
Time: 10:00 AM PDT
Duration: Up to 45 Minutes""",
        datetime(2026, 9, 22, tzinfo=timezone.utc),
    )

    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 9, 29, 17, 45, tzinfo=timezone.utc)


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


def test_cross_thread_confirmation_hides_related_availability_and_it_stays_hidden():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="extreme-availability",
            thread_id="availability-thread",
            subject="Interview Request | Extreme Networks",
            sender="Yana <yana@extremenetworks.com>",
            received_at=datetime(2026, 9, 22, 14, tzinfo=timezone.utc),
            body_text="Would you be available September 29 between 10:00 AM and 12:00 PM PDT?",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Extreme Networks",
        )
        db.add(invitation)
        db.flush()
        assert asyncio.run(process_calendar_email(db, invitation, use_ai=False)) is True
        available = db.query(CalendarEvent).filter(CalendarEvent.kind == "availability").one()

        confirmation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="extreme-confirmation",
            thread_id="confirmation-thread",
            subject="Interview with Extreme Networks",
            sender="Yana <yana@extremenetworks.com>",
            received_at=datetime(2026, 9, 22, 15, tzinfo=timezone.utc),
            body_text="Date: Tuesday, 29th September\nTime: 10:00 AM PDT\nDuration: Up to 45 Minutes",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Extreme Networks",
        )
        db.add(confirmation)
        db.flush()

        assert asyncio.run(process_calendar_email(db, confirmation, use_ai=False)) is True
        scheduled = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").one()
        assert scheduled.start_at == datetime(2026, 9, 29, 17, tzinfo=timezone.utc)
        assert scheduled.end_at == datetime(2026, 9, 29, 17, 45, tzinfo=timezone.utc)
        assert available.is_visible is False
        assert available.superseded_by_event_id == scheduled.id

        assert asyncio.run(process_calendar_email(db, invitation, force=True, use_ai=False)) is True
        assert available.is_visible is False


def test_scheduled_invite_hides_overlapping_availability_from_another_role_string():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="window",
            thread_id="offer-thread",
            subject="Share your availability",
            sender="recruiter@other.test",
            received_at=datetime(2026, 9, 22, 14, tzinfo=timezone.utc),
            body_text="Would Tuesday, 29 September between 10:00 AM and 12:00 PM PDT work?",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Extreme Networks",
            job_role="Innovation Lead",
        )
        db.add(invitation)
        db.flush()
        assert asyncio.run(process_calendar_email(db, invitation, use_ai=False)) is True

        confirmation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="booked",
            thread_id="invite-thread",
            subject="Interview with Extreme Networks",
            sender="scheduler@calendar.test",
            received_at=datetime(2026, 9, 22, 15, tzinfo=timezone.utc),
            body_text=(
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
                "DTSTART;TZID=Pacific Standard Time:20260929T100000\n"
                "DTEND;TZID=Pacific Standard Time:20260929T104500\n"
                "SUMMARY:Interview with Extreme Networks\nEND:VEVENT\nEND:VCALENDAR"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Extreme Networks",
            job_role="Office of the CTO",
        )
        db.add(confirmation)
        db.flush()
        assert asyncio.run(process_calendar_email(db, confirmation, use_ai=False)) is True
        scheduled = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed", CalendarEvent.is_visible.is_(True)).one()
        assert scheduled.start_at == datetime(2026, 9, 29, 17, tzinfo=timezone.utc)
        assert scheduled.end_at == datetime(2026, 9, 29, 17, 45, tzinfo=timezone.utc)
        available = db.query(CalendarEvent).filter(CalendarEvent.kind == "availability").one()
        assert available.is_visible is False
        assert available.superseded_by_event_id == scheduled.id


def test_confirmation_hides_earlier_availability_on_another_day():
    factory, (mailbox_id, _) = _database()
    role = "Innovation Lead - Office of the CTO"
    with factory() as db:
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="extreme-open-window",
            thread_id="offer-thread",
            subject="Re: Interview Request | Extreme Networks",
            sender="Yana <ysylvester@extremenetworks.com>",
            received_at=datetime(2026, 9, 22, 14, tzinfo=timezone.utc),
            body_text="Monday, September 28, 2026 from 12:00 PM to 6:00 PM ET still works on my side.",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Extreme Networks",
            job_role=role,
        )
        db.add(invitation)
        db.flush()
        assert asyncio.run(process_calendar_email(db, invitation, use_ai=False)) is True
        available = db.query(CalendarEvent).filter(CalendarEvent.kind == "availability", CalendarEvent.is_visible.is_(True)).one()
        assert available.start_at == datetime(2026, 9, 28, 16, tzinfo=timezone.utc)
        assert available.end_at == datetime(2026, 9, 28, 22, tzinfo=timezone.utc)

        confirmation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="extreme-booked",
            thread_id="booked-thread",
            subject="Interview with Extreme Networks",
            sender="Yana <ysylvester@extremenetworks.com>",
            received_at=datetime(2026, 9, 23, 3, tzinfo=timezone.utc),
            body_text=(
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
                "DTSTART;TZID=Pacific Standard Time:20260929T100000\n"
                "DTEND;TZID=Pacific Standard Time:20260929T104500\n"
                "END:VEVENT\nEND:VCALENDAR"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Extreme Networks",
            job_role=role,
        )
        db.add(confirmation)
        db.flush()
        assert asyncio.run(process_calendar_email(db, confirmation, use_ai=False)) is True
        scheduled = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed", CalendarEvent.is_visible.is_(True)).one()
        assert scheduled.start_at == datetime(2026, 9, 29, 17, tzinfo=timezone.utc)
        assert scheduled.end_at == datetime(2026, 9, 29, 17, 45, tzinfo=timezone.utc)
        assert available.is_visible is False
        assert available.superseded_by_event_id == scheduled.id
        assert needs_calendar_event_repair(db, confirmation) is False

        assert asyncio.run(process_calendar_email(db, invitation, force=True, use_ai=False)) is True
        assert available.is_visible is False


def test_scheduled_event_with_wrong_year_or_duration_is_selected_for_repair():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="wrong-year-confirmation",
            thread_id="wrong-year-thread",
            subject="Interview with Extreme Networks",
            sender="Yana <yana@extremenetworks.com>",
            received_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
            body_text="Date: Tuesday, 29th September\nTime: 10:00 AM PDT\nDuration: Up to 45 Minutes",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Extreme Networks",
        )
        db.add(email)
        db.flush()
        event = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=email.id,
            source_thread_id=email.thread_id,
            kind="confirmed",
            origin="email",
            title="Extreme Networks",
            start_at=datetime(2023, 9, 29, 17, tzinfo=timezone.utc),
            end_at=datetime(2023, 9, 29, 18, tzinfo=timezone.utc),
            is_visible=True,
        )
        db.add(event)
        db.flush()

        assert needs_calendar_event_repair(db, email) is True
        assert asyncio.run(process_calendar_email(db, email, force=True, use_ai=False)) is True
        assert event.start_at == datetime(2026, 9, 29, 17, tzinfo=timezone.utc)
        assert event.end_at == datetime(2026, 9, 29, 17, 45, tzinfo=timezone.utc)
        assert needs_calendar_event_repair(db, email) is False

        event.end_at = datetime(2026, 9, 29, 18, tzinfo=timezone.utc)
        db.flush()
        assert needs_calendar_event_repair(db, email) is True
        assert asyncio.run(process_calendar_email(db, email, force=True, use_ai=False)) is True
        assert event.end_at == datetime(2026, 9, 29, 17, 45, tzinfo=timezone.utc)


def test_positive_outcome_marks_existing_calendar_event_passed():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        event = CalendarEvent(mailbox_id=mailbox_id, source_thread_id="pass-thread", kind="confirmed", origin="email", title="Acme interview", company="Acme", job_role="Engineer", start_at=datetime(2026, 10, 5, 14, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc), is_visible=True)
        db.add(event)
        db.flush()
        outcome = EmailMessage(mailbox_id=mailbox_id, provider_message_id="pass-outcome", thread_id="pass-thread", subject="You passed the interview", sender="recruiter@acme.test", body_text="We are pleased to move you forward to the next round.", label=EmailLabel.INTERVIEW_FOLLOW_UP.value, company="Acme", job_role="Engineer")
        db.add(outcome)
        db.flush()

        assert asyncio.run(process_calendar_email(db, outcome)) is True
        assert event.application_status == "passed"
        assert event.outcome_source_email_id == outcome.id
        assert db.get(CalendarEventEmail, (event.id, outcome.id)) is not None


def test_offer_marks_existing_calendar_event_passed():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        event = CalendarEvent(mailbox_id=mailbox_id, source_thread_id="interview-thread", kind="confirmed", origin="email", title="Acme interview", company="Acme", job_role="Engineer", start_at=datetime(2026, 10, 5, 14, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc), is_visible=True)
        db.add(event)
        db.flush()
        offer = EmailMessage(mailbox_id=mailbox_id, provider_message_id="offer-outcome", thread_id="offer-thread", subject="Your offer from Acme", sender="recruiter@acme.test", body_text="We are pleased to offer you the role.", label=EmailLabel.OFFER.value, company="Acme", job_role="Engineer")
        db.add(offer)
        db.flush()

        assert asyncio.run(process_calendar_email(db, offer)) is True
        assert event.application_status == "passed"


def test_neutral_follow_up_does_not_mark_calendar_event_passed():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        event = CalendarEvent(mailbox_id=mailbox_id, source_thread_id="neutral-thread", kind="confirmed", origin="email", title="Acme interview", start_at=datetime(2026, 10, 5, 14, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc), is_visible=True)
        db.add(event)
        db.flush()
        follow_up = EmailMessage(mailbox_id=mailbox_id, provider_message_id="neutral-follow-up", thread_id="neutral-thread", subject="Interview feedback survey", sender="recruiter@acme.test", body_text="Please tell us about your interview experience.", label=EmailLabel.INTERVIEW_FOLLOW_UP.value)
        db.add(follow_up)
        db.flush()

        assert asyncio.run(process_calendar_email(db, follow_up)) is False
        assert event.application_status == "active"


def test_cancellation_keeps_the_existing_confirmed_event_visible_with_status():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(mailbox_id=mailbox_id, provider_message_id="cancel-source", thread_id="cancel-thread", subject="Canceled interview", sender="recruiter@example.com", body_text="Your interview on October 5, 2026 at 2:00 PM UTC was canceled.", label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="cancellation", company="Acme")
        db.add(email)
        db.flush()
        event = CalendarEvent(mailbox_id=mailbox_id, source_email_id=email.id, source_thread_id=email.thread_id, kind="confirmed", origin="email", title="Acme", start_at=datetime(2026, 10, 5, 14, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc), is_visible=True)
        db.add(event)
        db.flush()

        assert asyncio.run(process_calendar_email(db, email, force=True, use_ai=False)) is True
        assert event.interview_status == "cancellation"
        assert event.is_visible is True


def test_hidden_cancellation_is_selected_for_repair_and_restored():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(mailbox_id=mailbox_id, provider_message_id="hidden-cancel-source", thread_id="hidden-cancel-thread", subject="Meeting canceled", sender="recruiter@example.com", body_text="This meeting has been canceled.", label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="cancellation", company="Acme")
        db.add(email)
        db.flush()
        event = CalendarEvent(mailbox_id=mailbox_id, source_email_id=email.id, source_thread_id=email.thread_id, kind="confirmed", origin="email", title="Acme", start_at=datetime(2026, 10, 5, 14, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc), interview_status="cancellation", is_visible=False)
        db.add(event)
        db.flush()

        assert needs_calendar_event_repair(db, email) is True
        assert asyncio.run(process_calendar_email(db, email, force=True, use_ai=False)) is True
        assert event.interview_status == "cancellation"
        assert event.is_visible is True
        assert needs_calendar_event_repair(db, email) is False


def test_mislabeled_cancel_file_marks_the_booked_interview_canceled_and_stays_canceled():
    factory, (mailbox_id, _) = _database()
    invite = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
SUMMARY:Anomali
DTSTART;TZID=Eastern Standard Time:20261001T090000
DTEND;TZID=Eastern Standard Time:20261001T094500
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
You are all set on Thu, October 1st @ 9am EDT.
"""
    cancel = """BEGIN:VCALENDAR
METHOD:CANCEL
BEGIN:VEVENT
SUMMARY:Canceled: Anomali
DTSTART;TZID=Eastern Standard Time:20261001T090000
DTEND;TZID=Eastern Standard Time:20261001T094500
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
You are all set on Thu, October 1st @ 9am EDT.
"""
    with factory() as db:
        confirmation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="anomali-booked",
            thread_id="anomali-booked-thread",
            subject="Anomali",
            sender="Monika Dailey <mdailey@anomali.com>",
            received_at=datetime(2026, 9, 25, 12, 36, tzinfo=timezone.utc),
            body_text=invite,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Anomali",
        )
        db.add(confirmation)
        db.flush()
        assert asyncio.run(process_calendar_email(db, confirmation, use_ai=False)) is True
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed", CalendarEvent.is_visible.is_(True)).one()
        assert event.interview_status == "confirmation"
        assert event.start_at == datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)

        cancellation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="anomali-cancel",
            thread_id="anomali-cancel-thread",
            subject="Canceled: Anomali",
            sender="Monika Dailey <mdailey@anomali.com>",
            received_at=datetime(2026, 9, 28, 14, 9, tzinfo=timezone.utc),
            body_text=cancel,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Anomali",
        )
        db.add(cancellation)
        db.flush()

        assert needs_calendar_event_repair(db, cancellation) is True
        assert asyncio.run(process_calendar_email(db, cancellation, force=True, use_ai=False)) is True
        assert event.interview_status == "cancellation"
        assert event.is_visible is True
        assert event.start_at == datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
        assert needs_calendar_event_repair(db, cancellation) is False

        asyncio.run(process_calendar_email(db, confirmation, force=True, use_ai=False))
        assert event.interview_status == "cancellation"
        assert event.is_visible is True


def test_reschedule_options_cancel_the_booking_instead_of_a_proposed_time():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        booked = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="booked-source",
            thread_id="booked-thread",
            subject="Anomali",
            sender="recruiter@anomali.com",
            received_at=datetime(2026, 9, 25, 12, tzinfo=timezone.utc),
            body_text="You are all set on Thu, October 1st @ 9am EDT.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Anomali",
        )
        db.add(booked)
        db.flush()
        event = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=booked.id,
            source_thread_id=booked.thread_id,
            source_received_at=booked.received_at,
            kind="confirmed",
            origin="email",
            company="Anomali",
            job_role="VP, Product & Engineering",
            start_at=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 1, 13, 45, tzinfo=timezone.utc),
            interview_status="confirmation",
            is_visible=True,
        )
        db.add(event)
        db.flush()
        options = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="anomali-options",
            thread_id="anomali-options-thread",
            subject="Anomali",
            sender="recruiter@anomali.com",
            received_at=datetime(2026, 9, 28, 14, 18, tzinfo=timezone.utc),
            body_text=(
                "I just cancelled your interview. Updated availability below:\n"
                "Wed, Sep 30, 2026, 8:30 AM EDT\n"
                "Fri, Oct 2, 2026, 8:00 AM EDT\n"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="cancellation",
            company="Anomali",
            job_role="VP, Product & Engineering",
        )
        db.add(options)
        db.flush()
        invented = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=options.id,
            source_thread_id=options.thread_id,
            source_received_at=options.received_at,
            kind="confirmed",
            origin="email",
            company="Anomali",
            job_role="VP, Product & Engineering",
            start_at=datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc),
            interview_status="cancellation",
            is_visible=True,
        )
        db.add(invented)
        db.flush()

        assert needs_calendar_event_repair(db, options) is True
        assert asyncio.run(process_calendar_email(db, options, force=True, use_ai=False)) is True
        assert event.interview_status == "cancellation"
        assert event.is_visible is True
        assert event.start_at == datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
        assert invented.is_visible is False
        assert db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True), CalendarEvent.kind == "confirmed").count() == 1
        assert needs_calendar_event_repair(db, options) is False
        asyncio.run(process_calendar_email(db, options, force=True, use_ai=False))
        assert event.interview_status == "cancellation"
        assert event.is_visible is True
        assert event.start_at == datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
        assert invented.is_visible is False


def test_cancellation_with_a_time_creates_a_visible_calendar_event():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="standalone-cancel",
            thread_id="standalone-cancel-thread",
            subject="Interview canceled",
            sender="recruiter@example.com",
            body_text="Your interview on October 5, 2026 at 10:00 AM UTC was canceled.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="cancellation",
            company="Acme",
        )
        db.add(email)
        db.flush()

        assert asyncio.run(process_calendar_email(db, email, use_ai=False)) is True
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").one()
        assert event.interview_status == "cancellation"
        assert event.is_visible is True


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

    async def extract_without_slot(email, related_emails=None, **_kwargs):
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


def test_reschedule_without_a_new_time_removes_the_booked_interview():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        confirmation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="ludwig-confirm",
            thread_id="confirm-thread",
            subject="Re: My apologies",
            sender="Raj Yavatkar <raj@ludwigcomputing.com>",
            received_at=datetime(2026, 9, 25, 13, 53, tzinfo=timezone.utc),
            body_text="Great, talk to you then.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Ludwig Computing, Inc",
            job_role="Chief AI Officer and Co-founder",
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="ludwig-invite",
            thread_id="invite-thread",
            subject="Re: My apologies",
            sender="Raj Yavatkar <raj@ludwigcomputing.com>",
            received_at=datetime(2026, 9, 25, 13, 59, tzinfo=timezone.utc),
            body_text="Just sent you an invite.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Ludwig Computing, Inc",
            job_role="Chief AI Officer and Co-founder",
        )
        reschedule = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="ludwig-reschedule",
            thread_id="invite-thread",
            subject="Re: My apologies",
            sender="Raj Yavatkar <raj@ludwigcomputing.com>",
            received_at=datetime(2026, 9, 28, 15, 41, tzinfo=timezone.utc),
            body_text=(
                "hi Ryan i have a family emergency this AM and need to reschedule. i will be in touch\n"
                "________________________________\n"
                "From: Raj Yavatkar\n"
                "Sent: Friday, September 25, 2026 9:59 AM\n"
                "Monday, September 28, 2026, 1:00 PM - 1:30 PM EDT"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="reschedule",
        )
        db.add_all([confirmation, invite, reschedule])
        db.flush()
        booked = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=confirmation.id,
            source_thread_id=confirmation.thread_id,
            source_received_at=confirmation.received_at,
            kind="confirmed",
            origin="email",
            title="Ludwig Computing, Inc",
            company="Ludwig Computing, Inc",
            job_role="Chief AI Officer and Co-founder",
            start_at=datetime(2026, 9, 28, 17, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 28, 17, 30, tzinfo=timezone.utc),
            interview_status="confirmation",
            is_visible=True,
        )
        bogus = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=reschedule.id,
            source_thread_id=reschedule.thread_id,
            source_received_at=invite.received_at,
            kind="confirmed",
            origin="email",
            title="Ludwig Computing, Inc",
            company="Ludwig Computing, Inc",
            job_role="Chief AI Officer and Co-founder",
            start_at=invite.received_at,
            end_at=invite.received_at + timedelta(minutes=30),
            interview_status="reschedule",
            is_visible=True,
        )
        db.add_all([booked, bogus])
        db.flush()

        assert asyncio.run(process_calendar_email(db, reschedule, force=True, use_ai=False)) is True
        assert booked.is_visible is False
        assert bogus.is_visible is False
        confirmation.body_text = "Great, talk to you then. Monday, September 28, 2026 at 1:00 PM EDT"
        assert needs_calendar_event_repair(db, confirmation) is False
        assert db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True)).count() == 0

        asyncio.run(process_calendar_email(db, confirmation, force=True, use_ai=False))
        assert booked.is_visible is False
        assert db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True)).count() == 0


def test_existing_reschedule_without_a_new_time_is_hidden_on_repair():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        confirmation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="repair-confirm",
            thread_id="repair-confirm-thread",
            subject="Confirmed",
            sender="Raj Yavatkar <raj@ludwigcomputing.com>",
            received_at=datetime(2026, 9, 25, 13, 53, tzinfo=timezone.utc),
            body_text="Great, talk to you then.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Ludwig Computing, Inc",
            job_role="Chief AI Officer and Co-founder",
        )
        reschedule = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="repair-reschedule",
            thread_id="repair-invite-thread",
            subject="Re: My apologies",
            sender="Raj Yavatkar <raj@ludwigcomputing.com>",
            received_at=datetime(2026, 9, 28, 15, 41, tzinfo=timezone.utc),
            body_text="I need to reschedule. I will be in touch.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="reschedule",
        )
        db.add_all([confirmation, reschedule])
        db.flush()
        event = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=confirmation.id,
            source_thread_id=confirmation.thread_id,
            kind="confirmed",
            origin="email",
            company="Ludwig Computing, Inc",
            job_role="Chief AI Officer and Co-founder",
            start_at=datetime(2026, 9, 28, 17, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 28, 17, 30, tzinfo=timezone.utc),
            interview_status="confirmation",
            is_visible=True,
        )
        db.add(event)
        db.flush()
        db.add(CalendarEventEmail(event_id=event.id, email_id=reschedule.id))
        db.flush()

        assert hide_open_reschedules(db) == 1
        assert event.is_visible is False
        assert event.interview_status == "reschedule"


def test_reschedule_that_names_a_new_time_or_confirms_the_booking_stays_visible():
    moved = EmailMessage(
        subject="Kitware Call: 9:15 a.m.",
        body_text="Hoping OK that I moved our call to 9:15 a.m.",
        interview_subtype="reschedule",
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
    )
    confirmed = EmailMessage(
        subject="Scheduled: Ryan Queen <> USAA",
        body_text="Your meeting with USAA has been scheduled. It has links to cancel or reschedule the meeting.",
        interview_subtype="reschedule",
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
    )
    dated = EmailMessage(
        subject="Call",
        body_text="Your call has been rescheduled to Wednesday, 19/08 at 9:30 EST.",
        interview_subtype="reschedule",
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
    )
    assert leaves_interview_unbooked(moved) is False
    assert leaves_interview_unbooked(confirmed) is False
    assert leaves_interview_unbooked(dated) is False


def test_reschedule_to_a_new_day_hides_the_prior_booking():
    from app.services.calendar_extract import hide_superseded_identity_bookings

    factory, (mailbox_id, _) = _database()
    with factory() as db:
        older_mail = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="rivet-old",
            thread_id="rivet-thread",
            subject="Interview with Rivet Industries",
            sender="Manuela <manuela@rivet.us>",
            received_at=datetime(2026, 10, 1, 14, 51, tzinfo=timezone.utc),
            body_text="Interview invite for Mon, Oct 12, 2026 at 12:00 PM EDT.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Rivet Industries",
            job_role="ML Researcher",
        )
        newer_mail = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="rivet-new",
            thread_id="rivet-thread",
            subject="RE: Next Steps with Rivet Industries - ML Researcher",
            sender="Manuela <manuela@rivet.us>",
            received_at=datetime(2026, 10, 5, 17, 29, tzinfo=timezone.utc),
            body_text="Your interview was rescheduled due to Monday's holiday! I just sent you an interview invite for Tue, Oct 13, 2026 at 12:00 PM EDT.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="reschedule",
            company="Rivet Industries",
            job_role="ML Researcher",
        )
        db.add_all([older_mail, newer_mail])
        db.flush()
        old = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=older_mail.id,
            kind="confirmed",
            origin="email",
            company="Rivet Industries",
            job_role="ML Researcher",
            start_at=datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 12, 16, 15, tzinfo=timezone.utc),
            interview_status="calendar_invite",
            application_status="active",
            source_received_at=older_mail.received_at,
            is_visible=True,
        )
        new = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=newer_mail.id,
            kind="confirmed",
            origin="email",
            company="Rivet Industries",
            job_role="ML Researcher",
            start_at=datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 13, 16, 15, tzinfo=timezone.utc),
            interview_status="reschedule",
            application_status="active",
            source_received_at=newer_mail.received_at,
            is_visible=True,
        )
        db.add_all([old, new])
        db.commit()

        assert hide_superseded_identity_bookings(db) >= 1
        db.commit()
        assert new.is_visible is True
        assert old.is_visible is False
        assert old.superseded_by_event_id == new.id


def test_calendar_invite_fills_company_and_role_from_the_same_employer():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        application = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="itility-application",
            thread_id="itility-application-thread",
            subject="Thank you for your application, Ryan.",
            sender='"Itility B.V./Senior ML Engineer" <jobs@recruitee.com>',
            received_at=datetime(2026, 10, 1, 8, 12, tzinfo=timezone.utc),
            body_text="Thank you for applying for the position of Senior ML Engineer.",
            label=EmailLabel.APPLICATION_CONFIRMATION.value,
            company="Itility B.V.",
            job_role="Senior ML Engineer",
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="itility-invite",
            thread_id="itility-invite-thread",
            subject="Chat with itility",
            sender="Jasper Westerveld <jasper.westerveld@us-itility.com>",
            received_at=datetime(2026, 10, 1, 20, 24, tzinfo=timezone.utc),
            body_text=(
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
                "DTSTART:20261001T203000Z\nDTEND:20261001T210000Z\n"
                "END:VEVENT\nEND:VCALENDAR"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
        )
        db.add_all([application, invite])
        db.flush()

        assert asyncio.run(process_calendar_email(db, invite, use_ai=False)) is True
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").one()
        assert event.company == "Itility B.V."
        assert event.job_role == "Senior ML Engineer"


def test_missing_role_comes_from_the_latest_open_application_for_that_employer():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        older = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="floqast-core",
            thread_id="floqast-core-thread",
            subject="Thank you for your application to FloQast",
            sender="FloQast <no-reply@hire.lever.co>",
            received_at=datetime(2026, 9, 14, 12, tzinfo=timezone.utc),
            body_text="Application received.",
            label=EmailLabel.APPLICATION_CONFIRMATION.value,
            company="FloQast",
            job_role="Staff Software Engineer, Core AI",
        )
        rejected = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="floqast-old-reject",
            thread_id="floqast-old-thread",
            subject="Update on your application",
            sender="FloQast <no-reply@hire.lever.co>",
            received_at=datetime(2026, 9, 10, 12, tzinfo=timezone.utc),
            body_text="We will not be moving forward.",
            label=EmailLabel.REJECTED_CLOSED.value,
            company="FloQast",
            job_role="Senior Software Engineer, AI",
        )
        later = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="floqast-later",
            thread_id="floqast-later-thread",
            subject="Thank you for your application to FloQast",
            sender="FloQast <no-reply@hire.lever.co>",
            received_at=datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
            body_text="Application received.",
            label=EmailLabel.APPLICATION_CONFIRMATION.value,
            company="FloQast",
            job_role="Senior Software Engineer, AI",
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="floqast-confirm",
            thread_id="floqast-confirm-thread",
            subject="Confirmation: Your Interview Scheduled for Fri, 9/18/26 EST",
            sender="FloQast <no-reply@hire.lever.co>",
            received_at=datetime(2026, 9, 18, 15, tzinfo=timezone.utc),
            body_text=(
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
                "DTSTART:20260918T210000Z\nDTEND:20260918T213000Z\n"
                "END:VEVENT\nEND:VCALENDAR"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="FloQast",
        )
        db.add_all([older, rejected, later, invite])
        db.flush()

        assert asyncio.run(process_calendar_email(db, invite, use_ai=False)) is True
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").one()
        assert event.job_role == "Staff Software Engineer, Core AI"


def test_two_open_roles_in_the_same_week_are_not_guessed():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        first = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="waymo-a",
            thread_id="waymo-a",
            subject="Thank You for Applying to Waymo!",
            sender="Waymo <jobs@waymo.com>",
            received_at=datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
            label=EmailLabel.APPLICATION_CONFIRMATION.value,
            company="Waymo",
            job_role="Staff Machine Learning Engineer",
        )
        second = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="waymo-b",
            thread_id="waymo-b",
            subject="Thank You for Applying to Waymo!",
            sender="Waymo <jobs@waymo.com>",
            received_at=datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
            label=EmailLabel.APPLICATION_CONFIRMATION.value,
            company="Waymo",
            job_role="Senior Software Engineer",
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="waymo-call",
            thread_id="waymo-call",
            subject="Waymo Recruiter Call",
            sender="Elena Neve <eneve@waymo.com>",
            received_at=datetime(2026, 7, 29, 15, tzinfo=timezone.utc),
            body_text=(
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
                "DTSTART:20260729T180000Z\nDTEND:20260729T183000Z\n"
                "END:VEVENT\nEND:VCALENDAR"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Waymo",
        )
        db.add_all([first, second, invite])
        db.flush()

        assert asyncio.run(process_calendar_email(db, invite, use_ai=False)) is True
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").one()
        assert event.job_role == ""


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


def test_booking_uses_organizer_identity_to_enrich_and_link_separate_threads():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        availability = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="limrun-availability",
            thread_id="availability-thread",
            subject="Limrun Interview Availability",
            sender="no-reply@us.greenhouse-mail.io",
            received_at=datetime(2026, 9, 24, 19, 48, tzinfo=timezone.utc),
            body_text="Thanks for your interest in the Founding Engineer position at Limrun. Regards, Hasan",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Limrun",
            job_role="Founding Engineer",
        )
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="hasan-invitation",
            thread_id="invitation-thread",
            subject="Founding Engineer Interview | Mark & Hasan",
            sender='"Hasan Türken" <hasan@limrun.com>',
            received_at=datetime(2026, 9, 24, 20, 55, tzinfo=timezone.utc),
            body_text="We would love to move forward. Please pick a time using my calendar.",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="limrun.com",
            job_role="Founding Engineer",
        )
        other_interview = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="tunya-reminder",
            thread_id="other-interview-thread",
            subject="Reminder: Call with Tunya",
            sender='"Tunya Irkad" <tunya@limrun.com>',
            received_at=datetime(2026, 9, 24, 20, 40, tzinfo=timezone.utc),
            body_text="Your earlier Limrun interview starts soon.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="reminder",
            company="Limrun",
            job_role="Founding Engineer",
        )
        booking = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="hasan-booking",
            thread_id="booking-thread",
            subject="Appointment booked: 30 min with Hasan (Mark Wei)",
            sender='"Hasan Türken" <hasan@limrun.com>',
            received_at=datetime(2026, 9, 24, 20, 57, tzinfo=timezone.utc),
            body_text="""BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260928T190000Z
DTEND:20260928T193000Z
ORGANIZER:mailto:hasan@limrun.com
END:VEVENT
END:VCALENDAR""",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="",
            job_role="",
        )
        db.add_all([availability, invitation, other_interview, booking])
        db.flush()
        event = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=booking.id,
            source_thread_id=booking.thread_id,
            kind="confirmed",
            origin="email",
            title=booking.subject,
            company="",
            job_role="",
            start_at=datetime(2026, 9, 28, 19, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 28, 19, 30, tzinfo=timezone.utc),
            is_visible=True,
        )
        db.add(event)
        db.flush()

        assert refresh_calendar_event_details(db, booking) is True
        db.commit()
        db.refresh(event)
        event_id = event.id
        assert event.company == "Limrun"
        assert event.job_role == "Founding Engineer"
        assert event.title == "Limrun — Founding Engineer"
        expected_ids = [booking.id, invitation.id, availability.id]

    response = _client(factory).get(f"/api/calendar-events/{event_id}/emails")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == expected_ids


def test_teamtailor_reminder_keeps_role_and_meet_link_across_separate_threads(monkeypatch):
    factory, (mailbox_id, _) = _database()
    schedule_url = "https://careers.blueorange.digital/schedule/5d6de20f-e95c-443c-881a-ffaec8340694"

    async def resolve(_text: str):
        return MeetingAccess("video", "Google Meet", "https://meet.google.com/gwi-eeng-wvw")

    monkeypatch.setattr("app.services.calendar_extract._resolve_video_landing_page", resolve)

    with factory() as db:
        unrelated_role = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="blue-orange-unrelated-role",
            thread_id="other-role-thread",
            received_at=datetime(2026, 9, 18, 19, 8, tzinfo=timezone.utc),
            subject="Application Received for Sales Engineer role at Blue Orange",
            sender="Paula Barcelos <paula.barcelos@blueorangedigital.na.teamtailor-mail.com>",
            body_text="Thank you for applying to the Sales Engineer role.",
            label=EmailLabel.APPLICATION_CONFIRMATION.value,
            company="Blue Orange Digital",
            job_role="Sales Engineer",
        )
        role_context = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="blue-orange-role-context",
            thread_id="recruiter-thread",
            received_at=datetime(2026, 9, 25, 19, 8, tzinfo=timezone.utc),
            subject="Re: Time confirmed: Senior Data & LLM Engineer role at Blue Orange",
            sender="Paula Barcelos <paula.barcelos@blueorangedigital.na.teamtailor-mail.com>",
            body_text="I just sent you an updated invite.",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Blue Orange Digital",
            job_role="Senior Data & LLM Engineer (AI-Ready Data & Agentic Systems)",
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="blue-orange-invite",
            thread_id="calendar-thread",
            received_at=datetime(2026, 9, 25, 19, 9, tzinfo=timezone.utc),
            subject="Meeting invite: Ryan Queen / Paula Barcelos (Blue Orange Digital)",
            sender="Paula Barcelos <paula.barcelos@blueorangedigital.na.teamtailor-mail.com>",
            body_text=f"""Ryan Queen / Paula Barcelos (Blue Orange Digital)
DATE/TIME 2026-09-28 01:00 pm - 01:30 pm EDT
Your meeting will be a video call on Google Meet.
View meeting details {schedule_url}
Recruiting powered by Teamtailor""",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Blue Orange Digital",
            job_role="",
        )
        reminder = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="blue-orange-reminder",
            thread_id="reminder-thread",
            received_at=datetime(2026, 9, 27, 16, 1, tzinfo=timezone.utc),
            subject="Meeting reminder",
            sender="Blue Orange Digital <no-reply@blueorangedigital.na.teamtailor-mail.com>",
            body_text=f"""Meeting with Blue Orange Digital
DATE/TIME 2026-09-28 01:00 pm - 01:30 pm EDT
Your meeting will be a video call on Google Meet.
View meeting details {schedule_url}
Recruiting powered by Teamtailor""",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="reminder",
            company="Blue Orange Digital",
            job_role="",
        )
        db.add_all([unrelated_role, role_context, invite, reminder])
        db.flush()

        assert asyncio.run(process_calendar_email(db, invite, use_ai=False)) is True
        assert asyncio.run(process_calendar_email(db, reminder, use_ai=False)) is True
        db.commit()

        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").one()
        assert event.company == "Blue Orange Digital"
        assert event.job_role == "Senior Data & LLM Engineer (AI-Ready Data & Agentic Systems)"
        assert event.meeting_provider == "Google Meet"
        assert event.meeting_url == "https://meet.google.com/gwi-eeng-wvw"
        linked_ids = {
            item[0]
            for item in db.query(CalendarEventEmail.email_id)
            .filter(CalendarEventEmail.event_id == event.id)
            .all()
        }
        assert linked_ids == {role_context.id, invite.id, reminder.id}
        assert unrelated_role.id not in linked_ids


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


def test_company_variants_match_and_keep_the_fuller_name():
    assert companies_match("fanatics", "Fanatics Commerce")
    assert companies_match("PingWind", "PingWind, Inc.")
    assert companies_match("C3 AI", "c3.ai")
    assert companies_match("AlphaLife Sciences", "Alpha Life Sciences")
    assert companies_match("Gen Digital", "Gen Digital Inc.")
    assert fuller_company_name("fanatics", "Fanatics Commerce") == "Fanatics Commerce"
    assert not companies_match("Paraform", "Transparent Search Group")
    assert companies_match("Remy", "withremy")
    assert fuller_company_name("withremy", "Remy") == "Remy"
    assert not companies_match("Remy", "Acme")
    assert companies_match("collectfanatics", "Fanatics Collectibles")
    assert fuller_company_name("collectfanatics", "Fanatics Collectibles") == "Fanatics Collectibles"
    assert not companies_match("United Health", "United Airlines")
    assert employer_instead_of_sender_domain(
        EmailMessage(
            sender="Charisse Trotman <ctrotman@collectfanatics.com>",
            subject="Interview with Fanatics Collectibles",
            body_text="BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VEVENT\nEND:VCALENDAR",
        ),
        "collectfanatics",
    ) == "Fanatics Collectibles"


def test_company_name_drops_sentence_spill_after_the_employer():
    assert clean_company_name("CoreWeave. I") == "CoreWeave"
    assert clean_company_name("CoreWeave. I would like to set up a time") == "CoreWeave"
    assert clean_company_name("Roblox. We") == "Roblox"
    assert clean_company_name("WPP Media. We") == "WPP Media"
    assert clean_company_name("MSCI. I'd") == "MSCI"
    assert clean_company_name("Blank Metal. I") == "Blank Metal"
    assert clean_company_name("PingWind, Inc.") == "PingWind, Inc."
    assert explicit_hiring_employer(
        "Thank you for your interest in the Senior Forward Deployed Engineer, AI Agents role at CoreWeave. I would like to set up a time."
    ) == "CoreWeave"

def test_confirmation_and_domain_invite_collapse_to_one_chip():
    factory, (mailbox_id, _) = _database()
    start = datetime(2026, 10, 8, 16, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc)
    role = "Senior AI Engineer, Finance Systems"
    with factory() as db:
        confirmation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="fanatics-confirm-domain",
            thread_id="fanatics-confirm-domain",
            subject="Interview Confirmation — Senior AI Engineer, Finance Systems at Fanatics Collectibles",
            sender="Charisse Trotman <ctrotman@collectfanatics.com>",
            received_at=datetime(2026, 10, 5, 13, 59, tzinfo=timezone.utc),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Fanatics Collectibles",
            job_role=role,
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="fanatics-invite-domain",
            thread_id="fanatics-invite-domain",
            subject="Interview with Fanatics Collectibles",
            sender="Charisse Trotman <ctrotman@collectfanatics.com>",
            received_at=datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="collectfanatics",
            job_role=role,
        )
        db.add_all([confirmation, invite])
        db.flush()
        named = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=confirmation.id,
            kind="confirmed",
            origin="email",
            company="Fanatics Collectibles",
            job_role=role,
            title="Fanatics Collectibles — Senior AI Engineer, Finance Systems",
            start_at=start,
            end_at=end,
            interview_status="confirmation",
            application_status="active",
            source_received_at=confirmation.received_at,
            is_visible=True,
        )
        domain = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=invite.id,
            kind="confirmed",
            origin="email",
            company="collectfanatics",
            job_role=role,
            title="collectfanatics — Senior AI Engineer, Finance Systems",
            start_at=start,
            end_at=end,
            interview_status="calendar_invite",
            application_status="active",
            source_received_at=invite.received_at,
            is_visible=True,
        )
        db.add_all([named, domain])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        visible = (
            db.query(CalendarEvent)
            .filter(CalendarEvent.is_visible.is_(True), CalendarEvent.kind == "confirmed")
            .all()
        )
        assert len(visible) == 1
        assert visible[0].company == "Fanatics Collectibles"
        assert visible[0].job_role == role
        assert "collectfanatics" not in (visible[0].title or "").casefold()


def test_role_titles_match_when_a_middle_word_is_optional():
    from app.services.calendar_extract import roles_compatible

    assert roles_compatible("Principal Software Engineer", "Principal Engineer")
    assert roles_compatible("Lead Machine Learning Engineer", "Lead Engineer")
    assert not roles_compatible("Principal Software Engineer", "Staff Engineer")


def test_weekday_availability_window_uses_the_next_matching_day():
    received = datetime(2026, 10, 4, 18, 31, tzinfo=timezone.utc)  # Saturday
    slots = parse_text_slots("Tuesday from 2 PM - 3 PM PST works well for me.", received)
    assert len(slots) == 1
    assert slots[0].is_availability_window is True
    assert slots[0].start_at == datetime(2026, 10, 6, 21, tzinfo=timezone.utc)  # Tue 2pm PDT
    assert slots[0].end_at == datetime(2026, 10, 6, 22, tzinfo=timezone.utc)


def test_class_schedule_weekdays_are_not_interview_slots():
    received = datetime(2026, 9, 29, tzinfo=timezone.utc)
    slots = parse_text_slots(
        "Evenings(Mon,Wed & Thurs) from 7:30PM - 10:30PM EST. Weekend Saturday and Sunday (9AM - 1PM EST)",
        received,
    )
    assert slots == []


def test_sent_reply_without_interview_in_subject_still_creates_availability():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="bet-invite",
            thread_id="bet-thread",
            subject="bet365 Opportunity: Let's discuss your relocation",
            sender="Chris <chris@bet365.com>",
            received_at=datetime(2026, 10, 1, 13, tzinfo=timezone.utc),
            body_text="Are you free for a call?",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="bet365",
            job_role="AgentOps Technical Lead",
            folder="inbox",
        )
        reply = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="bet-reply",
            thread_id=None,
            subject="Re: bet365 Opportunity: Let's discuss your relocation",
            sender="me@example.com",
            received_at=datetime(2026, 10, 1, 17, tzinfo=timezone.utc),
            body_text="I am available on 10/7 at 12 PM PST. Please let me know if that time works.",
            label=EmailLabel.OTHER.value,
            folder="sent",
        )
        db.add_all([invite, reply])
        db.commit()
        assert asyncio.run(process_calendar_email(db, reply, use_ai=False)) is True
        db.commit()
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "availability", CalendarEvent.is_visible.is_(True)).one()
        assert event.start_at == datetime(2026, 10, 7, 19, tzinfo=timezone.utc)
        assert event.company == "bet365"
        assert event.job_role == "AgentOps Technical Lead"
        assert event.source_thread_id == "bet-thread"
        assert reply.thread_id == "bet-thread"
        assert reply.company == "bet365"


def test_today_city_times_use_the_city_timezone_not_utc():
    received = datetime(2026, 10, 6, 8, 14, tzinfo=timezone.utc)
    venice = parse_text_slots(
        "Would you be available today at 8am time in Venice, CA for a brief call?",
        received,
    )
    assert venice and venice[0].start_at == datetime(2026, 10, 6, 15, tzinfo=timezone.utc)
    assert venice[0].source_timezone == "America/Los_Angeles"
    tampa = parse_text_slots(
        "Would you be available today at 9am time in Tampa for a brief call?",
        received,
    )
    assert tampa and tampa[0].start_at == datetime(2026, 10, 6, 13, tzinfo=timezone.utc)
    assert tampa[0].source_timezone == "America/New_York"


def test_named_eastern_zone_beats_a_standard_offset_during_daylight_time():
    received = datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)
    slots = parse_text_slots(
        "When\nWednesday, October 7, 2026\n11:30 AM - 12:00 PM\n(UTC-05:00) Eastern Time (US & Canada)\n",
        received,
    )
    assert slots
    assert slots[0].start_at == datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 7, 16, tzinfo=timezone.utc)
    assert slots[0].source_timezone == "America/New_York"
    assert job_role_or_empty("empty") == ""
    assert clean_company_name("empty") == ""


def test_gmt_offset_without_minutes_is_not_misread_as_plain_utc():
    """"(GMT-7)" (no minutes) failed to match the offset regex, which required
    them, and fell through to the bare "GMT" alias — mapping straight to UTC
    and silently discarding the "-7". Confirmed on a real Google Calendar
    invite subject: "Tue Oct 13, 2026 1pm - 1:30pm (GMT-7)" was stored as
    1pm UTC instead of 1pm Pacific (8pm UTC), seven hours off."""
    received = datetime(2026, 10, 6, 4, 12, tzinfo=timezone.utc)
    slots = parse_text_slots(
        "Invitation: Interview with Picogrid @ Tue Oct 13, 2026 1pm - 1:30pm (GMT-7)",
        received,
    )
    assert slots
    assert slots[0].start_at == datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 13, 20, 30, tzinfo=timezone.utc)
    assert slots[0].source_timezone == "UTC-07:00"


def test_bst_abbreviation_is_recognized():
    """"BST" (British Summer Time) was entirely missing from the alias map,
    so a subject naming only that abbreviation produced zero deterministic
    slots and silently deferred to AI — which never runs in the fast bulk
    backfill pass, leaving the interview with no calendar chip at all."""
    received = datetime(2026, 10, 7, 18, 19, tzinfo=timezone.utc)
    slots = parse_text_slots(
        "Invitation: Thomas Lee and Christian Wood @ Wed 14 Oct 2026 2pm - 2:30pm (BST)",
        received,
    )
    assert slots
    assert slots[0].start_at == datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc)
    assert slots[0].source_timezone == "Europe/London"


def test_day_before_month_date_does_not_also_match_as_a_bare_weekday():
    """"Wednesday 14 Oct 2026" (day-before-month order) left the bare-weekday
    skip only recognizing the US "Wednesday, October 14" order, so the
    weekday word matched a second time on its own and resolved to the next
    Wednesday on/after the message's received_at — the same day when the
    message itself happened to arrive on a Wednesday, producing a bogus
    duplicate slot next to the correct explicit date."""
    received = datetime(2026, 10, 7, 18, 19, tzinfo=timezone.utc)  # also a Wednesday
    slots = parse_text_slots(
        "Invitation: Thomas Lee and Christian Wood @ Wed 14 Oct 2026 2pm - 2:30pm (BST)",
        received,
    )
    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc)


def test_reminder_joins_the_booking_instead_of_opening_a_second_chip():
    factory, (mailbox_id, _) = _database()
    booking = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261007T153000Z
DTEND:20261007T160000Z
SUMMARY:Interview
END:VEVENT
END:VCALENDAR
Join https://teams.microsoft.com/l/meetup-join/19:meeting"""
    reminder = """Hi Mark Wei,
You have an upcoming booking.
When
Wednesday, October 7, 2026
11:30 AM - 12:00 PM
(UTC-05:00) Eastern Time (US & Canada)
http://www.adusa.com/
"""
    with factory() as db:
        booked = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="ahold-booked",
            thread_id="ahold-book",
            subject="Confirmed: Interview",
            sender="Screening Availability <ScreeningAvailability@aholddelhaize.onmicrosoft.com>",
            received_at=datetime(2026, 10, 6, 17, 57, tzinfo=timezone.utc),
            body_text=booking,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Ahold Delhaize",
            job_role="Platform Administrator IV",
        )
        db.add(booked)
        db.flush()
        assert asyncio.run(process_calendar_email(db, booked, use_ai=False)) is True
        follow_up = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="ahold-reminder",
            thread_id="ahold-reminder",
            subject="Reminder: Interview - Mark Wei",
            sender="Screening Availability <ScreeningAvailability@aholddelhaize.onmicrosoft.com>",
            received_at=datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc),
            body_text=reminder,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="reminder",
            company="adusa.com",
            job_role="empty",
        )
        db.add(follow_up)
        db.flush()
        assert asyncio.run(process_calendar_email(db, follow_up, use_ai=False)) is True
        db.commit()
        visible = db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True), CalendarEvent.kind == "confirmed").all()
        assert len(visible) == 1
        assert visible[0].start_at == datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
        assert visible[0].end_at == datetime(2026, 10, 7, 16, tzinfo=timezone.utc)
        assert visible[0].company == "Ahold Delhaize"
        assert visible[0].job_role == "Platform Administrator IV"


def test_ics_london_invite_keeps_venice_pacific_label():
    """UK Outlook ICS + '8am in Venice, CA' must stay 8am PT / 11am ET."""
    factory, (mailbox_id, _) = _database()
    invite_body = """Hi Jordan,
As discussed, I'm sending this invitation for a brief call.
________________________________________________________________________________
Microsoft Teams meeting
Join: https://teams.microsoft.com/meet/3383917408431475?p=abc
BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VTIMEZONE
TZID:GMT Standard Time
END:VTIMEZONE
BEGIN:VEVENT
SUMMARY:Jordan Meeker / Senior AI Engineer
DTSTART;TZID=GMT Standard Time:20261006T160000
DTEND;TZID=GMT Standard Time:20261006T162000
END:VEVENT
END:VCALENDAR
"""
    with factory() as db:
        ask = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="colt-ask",
            thread_id="colt-mail",
            subject="RE: Senior AI Engineer / A Few Quick Questions from Colt",
            sender="Micaela <Micaela.SuarezdeTangil@colt.net>",
            received_at=datetime(2026, 10, 6, 8, 14, tzinfo=timezone.utc),
            body_text="Would you be available today at 8am time in Venice, CA for a brief call?",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Colt Technology Services",
            job_role="Senior AI Engineer",
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="colt-ics-zone",
            thread_id="colt-cal",
            subject="Jordan Meeker / Senior AI Engineer",
            sender="Micaela <Micaela.SuarezdeTangil@colt.net>",
            received_at=datetime(2026, 10, 6, 10, 45, tzinfo=timezone.utc),
            body_text=invite_body,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Colt Technology Services",
            job_role="Senior AI Engineer",
        )
        db.add_all([ask, invite])
        db.flush()
        assert asyncio.run(process_calendar_email(db, ask, use_ai=False)) is True
        assert asyncio.run(process_calendar_email(db, invite, use_ai=False)) is True
        db.commit()
        event = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed", CalendarEvent.is_visible.is_(True)).one()
        assert event.start_at == datetime(2026, 10, 6, 15, tzinfo=timezone.utc)
        assert event.source_timezone == "America/Los_Angeles"


def test_teams_invite_keeps_ics_time_when_follow_up_has_no_clock(monkeypatch):
    """Colt-style: ICS under a Teams rule line, then a 'just sent the invite' mail.

    The underscore rule must not strip the calendar file, the short Teams /meet/
    URL must be accepted, and a later follow-up must not invent a wrong time.
    """
    from app.services import calendar_extract as calendar_mod
    from app.services.calendar_extract import is_video_join_url

    short_join = "https://teams.microsoft.com/meet/3383917408431475?p=sFxcREuf9uVWxXD4Nz"
    assert is_video_join_url(short_join)
    assert not is_video_join_url("https://teams.microsoft.com/l/meetup-join/")

    invite_body = f"""Hi Jordan,

As discussed, I'm sending this invitation for a brief call.

I look forward to speaking with you.

Best regards,
Micaela
________________________________________________________________________________
Microsoft Teams meeting
Join: {short_join}
Meeting ID: 338 391 740 843 147 5
Passcode: Ki6EC9CW
________________________________
Need help?
BEGIN:VCALENDAR
METHOD:REQUEST
PRODID:Microsoft Exchange Server 2010
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Jordan Meeker / Senior AI Engineer
DTSTART;TZID=GMT Standard Time:20261006T160000
DTEND;TZID=GMT Standard Time:20261006T162000
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""
    assert "BEGIN:VCALENDAR" in _without_quoted_history(invite_body)
    factory, (mailbox_id, _) = _database()

    async def _fake_create(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "slots": [{
                                "start": "2026-10-06T23:00:00+00:00",
                                "end": "2026-10-06T23:20:00+00:00",
                                "timezone": "PDT",
                            }],
                            "company": "Colt Technology Services",
                            "role": "Senior AI Engineer",
                            "meeting_type": "video",
                            "meeting_provider": "Microsoft Teams",
                            "meeting_url": "https://teams.microsoft.com/l/meetup-join/",
                        })
                    )
                )
            ]
        )

    fake_backend = SimpleNamespace(
        api_key="test",
        model="fake",
        client=lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_fake_create))
        ),
    )
    monkeypatch.setattr(calendar_mod, "get_ai_backend", lambda: fake_backend)

    with factory() as db:
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="colt-ics",
            thread_id="colt-calendar-thread",
            subject="Jordan Meeker / Senior AI Engineer",
            sender="Micaela <Micaela.SuarezdeTangil@colt.net>",
            received_at=datetime(2026, 10, 6, 10, 45, tzinfo=timezone.utc),
            body_text=invite_body,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Colt Technology Services",
            job_role="Senior AI Engineer",
        )
        follow_up = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="colt-follow-up",
            thread_id="colt-email-thread",
            subject="RE: Senior AI Engineer / A Few Quick Questions from Colt",
            sender="Micaela <Micaela.SuarezdeTangil@colt.net>",
            received_at=datetime(2026, 10, 6, 10, 46, tzinfo=timezone.utc),
            body_text=(
                "Hi Jordan,\n\nThank you for your confirmation, I have just sent you the invite. "
                "During the meeting, we will complete identity verification.\n\nBest regards,\nMicaela"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="calendar_invite",
            company="Colt Technology Services",
            job_role="Senior AI Engineer",
        )
        db.add_all([invite, follow_up])
        db.flush()
        assert message_is_booked_appointment(invite) is True
        slots = message_appointment_slots(invite)
        assert slots and slots[0].start_at == datetime(2026, 10, 6, 15, tzinfo=timezone.utc)
        assert extract_meeting_access(invite_body).url == short_join

        assert asyncio.run(process_calendar_email(db, invite, use_ai=False)) is True
        # Follow-up has no clock of its own; AI must not invent 23:00 UTC and
        # displace the ICS booking. No new event is required from that mail.
        asyncio.run(process_calendar_email(db, follow_up, use_ai=True))
        db.commit()
        collapse_duplicate_calendar_events(db, mailbox_id)
        db.commit()

        visible = (
            db.query(CalendarEvent)
            .filter(CalendarEvent.kind == "confirmed", CalendarEvent.is_visible.is_(True))
            .all()
        )
        assert len(visible) == 1
        event = visible[0]
        assert event.start_at == datetime(2026, 10, 6, 15, tzinfo=timezone.utc)
        assert event.end_at == datetime(2026, 10, 6, 15, 20, tzinfo=timezone.utc)
        assert event.meeting_url == short_join
        assert event.source_email_id == invite.id
        wrong = datetime(2026, 10, 6, 23, tzinfo=timezone.utc)
        assert not any(
            item.is_visible and item.start_at == wrong
            for item in db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed").all()
        )


def test_time_before_the_date_is_not_read_as_the_end_clock():
    received = datetime(2026, 9, 4, tzinfo=timezone.utc)
    slots = parse_text_slots(
        "Please move the call to 1:00-1:30 PM PT on Wednesday, September 10, 2026.",
        received,
    )
    assert slots[0].start_at == datetime(2026, 9, 10, 20, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 9, 10, 20, 30, tzinfo=timezone.utc)
    assert slots[0].is_availability_window is False


def test_availability_window_is_not_the_booked_call():
    received = datetime(2026, 9, 4, tzinfo=timezone.utc)
    window = parse_text_slots(
        "I am available on September 10, 2026 between 12:00 PM and 3:00 PM PT.",
        received,
    )
    assert len(window) == 1
    assert window[0].is_availability_window is True
    assert window[0].start_at == datetime(2026, 9, 10, 19, tzinfo=timezone.utc)
    assert window[0].end_at == datetime(2026, 9, 10, 22, tzinfo=timezone.utc)

    email = EmailMessage(
        sender="Puja <puja@7-11.com>",
        subject="RE: 7 Eleven - Staff Engineer",
        received_at=received,
        body_text="Sure, I will move the call at 1pm PT.",
        body_html=(
            "Sure, I will move the call at 1pm PT."
            "<b>Sent:</b> Friday, September 4, 2026 11:47 AM<br>"
            "I will call you on Wednesday 9/10 at 12:30pm PT. "
            "I am available on September 10 between 12:00 PM and 3:00 PM PT."
        ),
    )
    slots = message_appointment_slots(email)
    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 9, 10, 20, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 9, 10, 20, 30, tzinfo=timezone.utc)


def test_quoted_window_on_a_saved_interview_is_replaced_by_the_new_time():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="seven",
            thread_id="seven",
            subject="RE: 7 Eleven",
            sender="Puja <puja@7-11.com>",
            received_at=datetime(2026, 9, 4, 16, tzinfo=timezone.utc),
            body_text="Sure, I will move the call at 1pm PT.",
            body_html=(
                "Sure, I will move the call at 1pm PT.\n"
                "<b>Sent:</b> Friday, September 4, 2026 11:47 AM<br>\n"
                "I am available on September 10 between 12:00 PM and 3:00 PM PT."
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="reschedule",
            company="7-ELEVEN Inc.",
        )
        db.add(email)
        db.flush()
        event = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=email.id,
            kind="confirmed",
            origin="email",
            company="7-ELEVEN Inc.",
            job_role="Staff Engineer - AI Agent",
            start_at=datetime(2026, 9, 10, 19, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 10, 22, tzinfo=timezone.utc),
            interview_status="reschedule",
            application_status="active",
            is_visible=True,
        )
        db.add(event)
        db.commit()
        assert repair_availability_window_appointments(db) == 1
        db.commit()
        assert event.start_at == datetime(2026, 9, 10, 20, tzinfo=timezone.utc)
        assert event.end_at == datetime(2026, 9, 10, 20, 30, tzinfo=timezone.utc)


def test_scheduler_domain_collapses_onto_the_employer_name():
    factory, (mailbox_id, _) = _database()
    start = datetime(2026, 9, 10, 19, 30, tzinfo=timezone.utc)
    end = datetime(2026, 9, 10, 20, tzinfo=timezone.utc)
    with factory() as db:
        booked = CalendarEvent(
            mailbox_id=mailbox_id,
            kind="confirmed",
            origin="email",
            company="Remy",
            job_role="Founding AI/ML Engineer",
            start_at=start,
            end_at=end,
            interview_status="confirmation",
            application_status="active",
            source_received_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
            is_visible=True,
        )
        reminder = CalendarEvent(
            mailbox_id=mailbox_id,
            kind="confirmed",
            origin="email",
            company="withremy",
            job_role="",
            start_at=start,
            end_at=end,
            interview_status="reminder",
            application_status="active",
            source_received_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
            is_visible=True,
        )
        db.add_all([booked, reminder])
        db.commit()
        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        visible = db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True)).all()
        assert len(visible) == 1
        assert visible[0].id == reminder.id
        assert visible[0].company == "Remy"
        assert visible[0].job_role == "Founding AI/ML Engineer"
        assert booked.is_visible is False


def test_sender_domain_does_not_replace_the_named_employer():
    tapestry = EmailMessage(
        sender="Greg Krouse <gkrouse@google.com>",
        subject="Recruiter Screen Confirmation: Staff Backend Software Engineer",
        body_text="Thanks for submitting your availability to discuss the Staff Backend Software Engineer position with Tapestry!",
    )
    assert employer_instead_of_sender_domain(tapestry, "Google") == "Tapestry"
    google = EmailMessage(
        sender="Recruiter <recruiter@google.com>",
        subject="Your interview",
        body_text="Your interview for the Software Engineer position with Google is confirmed.",
    )
    assert employer_instead_of_sender_domain(google, "Google") == "Google"
    clipped = EmailMessage(
        sender="Ada <ada@coreweave.com>",
        subject="Interview",
        body_text="Confirming the Senior Engineer position with Corewe tomorrow.",
    )
    assert employer_instead_of_sender_domain(clipped, "CoreWeave") == "CoreWeave"


def test_same_clock_slot_collapses_role_and_company_variants():
    factory, (mailbox_id, _) = _database()
    start = datetime(2026, 9, 21, 16, 15, tzinfo=timezone.utc)
    end = datetime(2026, 9, 21, 17, tzinfo=timezone.utc)
    with factory() as db:
        confirmation = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="Gen Digital", job_role="Lead Machine Learning Engineer", start_at=start, end_at=end, interview_status="confirmation", application_status="active", source_received_at=datetime(2026, 9, 10, tzinfo=timezone.utc), is_visible=True)
        session = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="Gen Digital Inc.", job_role="Lead Machine Learning Engineer - Engine", start_at=start, end_at=end, interview_status="calendar_invite", application_status="active", source_received_at=datetime(2026, 9, 11, tzinfo=timezone.utc), is_visible=True)
        later_round = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="Gen Digital Inc.", job_role="Lead Machine Learning Engineer - Engine", start_at=datetime(2026, 9, 21, 17, tzinfo=timezone.utc), end_at=datetime(2026, 9, 21, 18, tzinfo=timezone.utc), interview_status="calendar_invite", application_status="active", source_received_at=datetime(2026, 9, 11, 1, tzinfo=timezone.utc), is_visible=True)
        dotted = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="c3.ai", job_role="Applications Engineering", start_at=datetime(2026, 8, 7, 18, tzinfo=timezone.utc), end_at=datetime(2026, 8, 7, 18, 30, tzinfo=timezone.utc), interview_status="confirmation", application_status="active", source_received_at=datetime(2026, 8, 1, tzinfo=timezone.utc), is_visible=True)
        spelled = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="C3 AI", job_role="Senior Software Engineer - Generative AI", start_at=datetime(2026, 8, 7, 18, tzinfo=timezone.utc), end_at=datetime(2026, 8, 7, 18, 30, tzinfo=timezone.utc), interview_status="confirmation", application_status="active", source_received_at=datetime(2026, 8, 2, tzinfo=timezone.utc), is_visible=True)
        other = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="Nordstrom", job_role="Senior Engineer", start_at=datetime(2026, 8, 7, 18, tzinfo=timezone.utc), end_at=datetime(2026, 8, 7, 18, 45, tzinfo=timezone.utc), interview_status="confirmation", application_status="active", source_received_at=datetime(2026, 8, 2, tzinfo=timezone.utc), is_visible=True)
        db.add_all([confirmation, session, later_round, dotted, spelled, other])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        visible = db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True), CalendarEvent.kind == "confirmed").all()
        gen = [event for event in visible if "gen" in event.company.casefold()]
        assert len(gen) == 2
        noon = next(event for event in gen if event.start_at == start)
        assert noon.company == "Gen Digital Inc."
        assert noon.job_role == "Lead Machine Learning Engineer - Engine"
        assert sum(1 for event in visible if "c3" in event.company.casefold()) == 1
        assert any(event.company == "Nordstrom" for event in visible)


def test_same_interview_with_different_company_text_collapses_to_one_item():
    factory, (mailbox_id, _) = _database()
    role = "Staff Software Engineer - AI/ML Platform"
    start = datetime(2026, 10, 5, 17, 15, tzinfo=timezone.utc)
    end = datetime(2026, 10, 5, 17, 45, tzinfo=timezone.utc)
    with factory() as db:
        invite = EmailMessage(mailbox_id=mailbox_id, provider_message_id="fanatics-invite", thread_id="fanatics-invite", subject="Interview with Fanatics Commerce", sender="rachel@fanatics.com", received_at=datetime(2026, 9, 30, 17, tzinfo=timezone.utc), body_text="Invite", label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="calendar_invite", company="fanatics", job_role=role)
        confirmation = EmailMessage(mailbox_id=mailbox_id, provider_message_id="fanatics-confirm", thread_id="fanatics-confirm", subject="Interview Confirmation", sender="rachel@fanatics.com", received_at=datetime(2026, 9, 30, 17, 51, tzinfo=timezone.utc), body_text="Confirmed", label=EmailLabel.INTERVIEW_SCHEDULED.value, interview_subtype="confirmation", company="Fanatics Commerce", job_role=role)
        db.add_all([invite, confirmation])
        db.flush()
        short = CalendarEvent(mailbox_id=mailbox_id, source_email_id=invite.id, kind="confirmed", origin="email", company="fanatics", job_role=role, start_at=start, end_at=end, interview_status="calendar_invite", application_status="active", source_received_at=invite.received_at, is_visible=True, meeting_type="video")
        full = CalendarEvent(mailbox_id=mailbox_id, source_email_id=confirmation.id, kind="confirmed", origin="email", company="Fanatics Commerce", job_role=role, start_at=start, end_at=end, interview_status="confirmation", application_status="active", source_received_at=confirmation.received_at, is_visible=True, meeting_type="video")
        db.add_all([short, full])
        db.flush()
        db.add_all([
            CalendarEventEmail(event_id=short.id, email_id=invite.id),
            CalendarEventEmail(event_id=full.id, email_id=confirmation.id),
        ])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        visible = db.query(CalendarEvent).filter(CalendarEvent.kind == "confirmed", CalendarEvent.is_visible.is_(True)).all()
        assert len(visible) == 1
        assert visible[0].company == "Fanatics Commerce"
        assert db.get(CalendarEventEmail, (visible[0].id, invite.id)) is not None
        assert db.get(CalendarEventEmail, (visible[0].id, confirmation.id)) is not None


def test_reminder_and_cancellation_collapse_onto_the_scheduled_interview():
    factory, (mailbox_id, _) = _database()
    start = datetime(2026, 9, 30, 17, tzinfo=timezone.utc)
    end = datetime(2026, 9, 30, 17, 30, tzinfo=timezone.utc)
    with factory() as db:
        confirmed = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="Rivian Automotive, LLC", job_role="Staff Software Engineer", start_at=start, end_at=end, interview_status="confirmation", application_status="active", source_received_at=datetime(2026, 9, 20, tzinfo=timezone.utc), is_visible=True)
        reminder = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="Rivian", job_role="Staff Software Engineer", start_at=start, end_at=end, interview_status="reminder", application_status="active", source_received_at=datetime(2026, 9, 29, tzinfo=timezone.utc), is_visible=True)
        other = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="Paraform", job_role="", start_at=datetime(2026, 9, 29, 13, tzinfo=timezone.utc), end_at=datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc), interview_status="cancellation", application_status="active", is_visible=True)
        neighbor = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="Transparent Search Group", job_role="AI Engineer", start_at=datetime(2026, 9, 29, 13, tzinfo=timezone.utc), end_at=datetime(2026, 9, 29, 13, 20, tzinfo=timezone.utc), interview_status="calendar_invite", application_status="rejected", is_visible=True)
        db.add_all([confirmed, reminder, other, neighbor])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        visible = db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True), CalendarEvent.kind == "confirmed").all()
        companies = sorted(event.company for event in visible)
        assert companies == ["Paraform", "Rivian Automotive, LLC", "Transparent Search Group"]
        rivian = next(event for event in visible if event.company == "Rivian Automotive, LLC")
        assert rivian.interview_status == "reminder"


def test_booked_interview_hides_earlier_window_when_the_offer_has_no_role():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        offered = CalendarEvent(
            mailbox_id=mailbox_id,
            kind="availability",
            origin="email",
            company="Pathos",
            job_role="",
            start_at=datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 30, 21, tzinfo=timezone.utc),
            source_received_at=datetime(2026, 9, 25, 19, 16, tzinfo=timezone.utc),
            is_visible=True,
        )
        booked = CalendarEvent(
            mailbox_id=mailbox_id,
            kind="confirmed",
            origin="email",
            company="Pathos",
            job_role="Staff Software / AI Engineer",
            start_at=datetime(2026, 9, 28, 20, 30, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 28, 21, tzinfo=timezone.utc),
            interview_status="reminder",
            source_received_at=datetime(2026, 9, 27, 20, 30, tzinfo=timezone.utc),
            is_visible=True,
        )
        other_role = CalendarEvent(
            mailbox_id=mailbox_id,
            kind="availability",
            origin="email",
            company="Pathos",
            job_role="Account Executive",
            start_at=datetime(2026, 10, 2, 15, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 2, 15, 30, tzinfo=timezone.utc),
            source_received_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
            is_visible=True,
        )
        later = CalendarEvent(
            mailbox_id=mailbox_id,
            kind="availability",
            origin="email",
            company="Pathos",
            job_role="",
            start_at=datetime(2026, 10, 6, 16, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc),
            source_received_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
            is_visible=True,
        )
        db.add_all([offered, booked, other_role, later])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        assert db.get(CalendarEvent, offered.id).is_visible is False
        assert db.get(CalendarEvent, offered.id).superseded_by_event_id == booked.id
        assert db.get(CalendarEvent, booked.id).is_visible is True
        assert db.get(CalendarEvent, other_role.id).is_visible is True
        assert db.get(CalendarEvent, later.id).is_visible is True


def test_earlier_availability_variant_hides_after_booking_and_a_later_offer_stays():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        earlier = CalendarEvent(mailbox_id=mailbox_id, kind="availability", origin="email", company="fanatics", job_role="Staff Software Engineer - AI/ML Platform", start_at=datetime(2026, 9, 30, 18, tzinfo=timezone.utc), end_at=datetime(2026, 9, 30, 19, tzinfo=timezone.utc), source_received_at=datetime(2026, 9, 20, tzinfo=timezone.utc), is_visible=True)
        booked = CalendarEvent(mailbox_id=mailbox_id, kind="confirmed", origin="email", company="Fanatics Commerce", job_role="Staff Software Engineer - AI/ML Platform", start_at=datetime(2026, 10, 5, 17, 15, tzinfo=timezone.utc), end_at=datetime(2026, 10, 5, 17, 45, tzinfo=timezone.utc), interview_status="confirmation", source_received_at=datetime(2026, 9, 30, 17, tzinfo=timezone.utc), is_visible=True)
        later = CalendarEvent(mailbox_id=mailbox_id, kind="availability", origin="email", company="Fanatics Commerce", job_role="Staff Software Engineer - AI/ML Platform", start_at=datetime(2026, 10, 8, 18, tzinfo=timezone.utc), end_at=datetime(2026, 10, 8, 18, 30, tzinfo=timezone.utc), source_received_at=datetime(2026, 10, 1, tzinfo=timezone.utc), is_visible=True)
        other_day = CalendarEvent(mailbox_id=mailbox_id, kind="availability", origin="email", company="Genesis10", job_role="Lead Software Engineer - Onsite", start_at=datetime(2026, 9, 30, 18, 30, tzinfo=timezone.utc), end_at=datetime(2026, 9, 30, 19, tzinfo=timezone.utc), source_received_at=datetime(2026, 9, 18, tzinfo=timezone.utc), is_visible=True)
        other_window = CalendarEvent(mailbox_id=mailbox_id, kind="availability", origin="email", company="genesis10", job_role="Lead Software Engineer - Onsite", start_at=datetime(2026, 10, 2, 18, tzinfo=timezone.utc), end_at=datetime(2026, 10, 2, 18, 30, tzinfo=timezone.utc), source_received_at=datetime(2026, 9, 18, tzinfo=timezone.utc), is_visible=True)
        db.add_all([earlier, booked, later, other_day, other_window])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        visible = db.query(CalendarEvent).filter(CalendarEvent.is_visible.is_(True)).all()
        kinds = sorted((event.kind, event.company, event.start_at) for event in visible)
        assert (earlier.id, False) == (earlier.id, db.get(CalendarEvent, earlier.id).is_visible)
        assert any(event.id == booked.id for event in visible)
        assert any(event.id == later.id for event in visible)
        assert any(event.company == "Genesis10" for event in visible)
        assert any(event.company == "genesis10" for event in visible)
        assert len(kinds) == 4


_BOOKED_ICS = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260922T123000
DTEND;TZID=America/New_York:20260922T130000
SUMMARY:Book time with me
END:VEVENT
END:VCALENDAR
Event Name: Book time with me
Location: This is a Zoom web conference.
https://synply-io.zoom.us/j/88048149665
"""

_DECLINED_ICS = """BEGIN:VCALENDAR
METHOD:REPLY
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260831T080000
DTEND;TZID=America/New_York:20260831T083000
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""

_CONFIRMED_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260818T153000
DTEND;TZID=America/New_York:20260818T160000
END:VEVENT
END:VCALENDAR
"""


def test_scheduler_confirmation_is_a_booked_interview_not_an_open_invitation():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="synply-booked",
            thread_id="synply-booked",
            subject="Book time with me - Ryan Queen and Mathius Ballesteros Pacheco",
            sender="Mathius Ballesteros Pacheco <mathius@synply.io>",
            received_at=datetime(2026, 9, 21, 19, 59, tzinfo=timezone.utc),
            body_text=_BOOKED_ICS,
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Synply",
            job_role="Principal AI Engineer",
        )
        invitation = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="synply-invite",
            thread_id="synply-invite",
            subject="Interview invitation: Principal AI Engineer",
            sender="noreply@taleri.es",
            received_at=datetime(2026, 9, 21, 17, 14, tzinfo=timezone.utc),
            body_text="Interview invitation: Principal AI Engineer at Synply. https://scheduler.zoom.us/mathius/hello",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Synply",
            job_role="Principal AI Engineer",
        )
        declined = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="fivecast-declined",
            thread_id="fivecast-declined",
            subject="Declined: Fivecast intro call: AI Engineer",
            sender="Nicole <nicole@fivecast.test>",
            received_at=datetime(2026, 8, 30, 15, tzinfo=timezone.utc),
            body_text=_DECLINED_ICS + "\nPlease reply with your availability for the week.",
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Fivecast",
        )
        db.add_all([email, invitation, declined])
        db.flush()

        assert message_is_booked_appointment(email) is True
        assert message_is_booked_appointment(invitation) is False
        assert message_is_booked_appointment(declined) is False
        assert asyncio.run(process_calendar_email(db, email, use_ai=False)) is True
        event = db.query(CalendarEvent).filter(CalendarEvent.source_email_id == email.id).one()
        assert event.kind == "confirmed"
        assert event.is_visible is True
        assert event.start_at == datetime(2026, 9, 22, 16, 30, tzinfo=timezone.utc)
        assert email.label == EmailLabel.INTERVIEW_SCHEDULED.value


def test_stored_booked_availability_is_promoted_and_a_decline_stays_open():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        booked = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="stored-booked",
            thread_id="stored-booked",
            subject="Book time with me - Ryan Queen and Mathius Ballesteros Pacheco",
            sender="Mathius <mathius@synply.io>",
            received_at=datetime(2026, 9, 21, 19, 59, tzinfo=timezone.utc),
            body_text=_BOOKED_ICS,
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Synply",
            job_role="Principal AI Engineer",
        )
        confirmed = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="stored-confirmed",
            thread_id="stored-confirmed",
            subject="Confirmed: Phone Interview | Adidev Technologies Inc | Tue Aug 18, 2026 3:30pm - 4:00pm",
            sender="recruiter@adidev.test",
            received_at=datetime(2026, 8, 17, 15, tzinfo=timezone.utc),
            body_text=_CONFIRMED_ICS,
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Adidev Technologies Inc",
        )
        declined = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="stored-declined",
            thread_id="stored-declined",
            subject="Declined: Fivecast intro call: AI Engineer",
            sender="Nicole <nicole@fivecast.test>",
            received_at=datetime(2026, 8, 30, 15, tzinfo=timezone.utc),
            body_text=_DECLINED_ICS,
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Fivecast",
        )
        db.add_all([booked, confirmed, declined])
        db.flush()
        rows = [
            CalendarEvent(mailbox_id=mailbox_id, source_email_id=booked.id, kind="availability", origin="email", company="Synply", job_role="Principal AI Engineer", start_at=datetime(2026, 9, 22, 16, 30, tzinfo=timezone.utc), end_at=datetime(2026, 9, 22, 17, tzinfo=timezone.utc), is_visible=True),
            CalendarEvent(mailbox_id=mailbox_id, source_email_id=confirmed.id, kind="availability", origin="email", company="Adidev Technologies Inc", start_at=datetime(2026, 8, 18, 19, 30, tzinfo=timezone.utc), end_at=datetime(2026, 8, 18, 20, tzinfo=timezone.utc), is_visible=True),
            CalendarEvent(mailbox_id=mailbox_id, source_email_id=declined.id, kind="availability", origin="email", company="Fivecast", start_at=datetime(2026, 8, 31, 12, tzinfo=timezone.utc), end_at=datetime(2026, 8, 31, 12, 30, tzinfo=timezone.utc), is_visible=True),
        ]
        db.add_all(rows)
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        synply, adidev, fivecast = (db.get(CalendarEvent, row.id) for row in rows)
        assert synply.kind == "confirmed"
        assert adidev.kind == "confirmed"
        assert fivecast.kind == "availability"
        assert db.get(EmailMessage, booked.id).label == EmailLabel.INTERVIEW_SCHEDULED.value
        assert db.get(EmailMessage, declined.id).label == EmailLabel.INTERVIEW_INVITATION.value


def test_updated_calendar_invite_replaces_the_earlier_copy():
    factory, (mailbox_id, _) = _database()
    ics = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
UID:same-meeting@google.com
DTSTART;TZID=America/Chicago:20260916T130000
DTEND;TZID=America/Chicago:20260916T140000
END:VEVENT
END:VCALENDAR
"""
    updated = ics.replace("T130000", "T133000").replace("T140000", "T143000")
    with factory() as db:
        original = EmailMessage(mailbox_id=mailbox_id, provider_message_id="uid-original", thread_id="uid-a", subject="Invitation from an unknown sender: Jordan and Melinda @ Wed Sep 16, 2026 1pm", sender="Jordan <jordan@example.com>", received_at=datetime(2026, 9, 10, 15, tzinfo=timezone.utc), body_text=ics, label=EmailLabel.INTERVIEW_INVITATION.value, company="Acme")
        revision = EmailMessage(mailbox_id=mailbox_id, provider_message_id="uid-updated", thread_id="uid-b", subject="Updated invitation from an unknown sender: Jordan and Melinda @ Wed Sep 16, 2026 1:30pm", sender="Jordan <jordan@example.com>", received_at=datetime(2026, 9, 12, 15, tzinfo=timezone.utc), body_text=updated, label=EmailLabel.INTERVIEW_INVITATION.value, company="Acme")
        db.add_all([original, revision])
        db.flush()
        first = CalendarEvent(mailbox_id=mailbox_id, source_email_id=original.id, kind="availability", origin="email", company="Acme", start_at=datetime(2026, 9, 16, 18, tzinfo=timezone.utc), end_at=datetime(2026, 9, 16, 19, tzinfo=timezone.utc), is_visible=True)
        second = CalendarEvent(mailbox_id=mailbox_id, source_email_id=revision.id, kind="availability", origin="email", company="Acme", start_at=datetime(2026, 9, 16, 18, 30, tzinfo=timezone.utc), end_at=datetime(2026, 9, 16, 19, 30, tzinfo=timezone.utc), is_visible=True)
        db.add_all([first, second])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        assert db.get(CalendarEvent, first.id).is_visible is False
        assert db.get(CalendarEvent, first.id).superseded_by_event_id == second.id
        assert db.get(CalendarEvent, second.id).is_visible is True
        assert db.get(CalendarEvent, second.id).kind == "confirmed"


def test_quoted_send_clock_is_not_a_second_interview():
    waiting = (
        "Hi Mark, I am waiting in the call, are you coming?\n\n"
        "On Thu, Sep 24, 2026 at 12:01 PM mark.abraham.wei <\n"
        "mark.abraham.wei@gmail.com> wrote:\n\n"
        "> No problem. See you shortly.\n"
    )
    assert "12:01" not in _without_quoted_history(waiting)
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        reply = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="limrun-waiting",
            thread_id="limrun-waiting",
            subject="Re: Next step: Founding Engineer at Limrun",
            sender="Tunya Irkad <tunya@limrun.com>",
            received_at=datetime(2026, 9, 24, 19, 9, tzinfo=timezone.utc),
            body_text=waiting,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Limrun",
            job_role="Founding Engineer",
        )
        booking = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="limrun-booked",
            thread_id="limrun-booked",
            subject="Appointment booked: Call with Tunya (Mark Wei) @ Thu Sep 24, 2026 2pm - 2:30pm (CDT)",
            sender="Tunya Irkad <tunya@limrun.com>",
            received_at=datetime(2026, 9, 22, 21, 56, tzinfo=timezone.utc),
            body_text="Thursday Sep 24, 2026, 2pm - 2:30pm Central Time - Chicago",
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Limrun",
            job_role="Founding Engineer",
        )
        db.add_all([reply, booking])
        db.flush()
        phantom = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=reply.id,
            kind="confirmed",
            origin="email",
            company="Limrun",
            job_role="Founding Engineer",
            start_at=datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 24, 16, 30, tzinfo=timezone.utc),
            is_visible=True,
        )
        real = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=booking.id,
            kind="confirmed",
            origin="email",
            company="Limrun",
            job_role="Founding Engineer",
            start_at=datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 24, 19, 30, tzinfo=timezone.utc),
            is_visible=True,
        )
        db.add_all([phantom, real])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        assert db.get(CalendarEvent, phantom.id).is_visible is False
        assert db.get(CalendarEvent, real.id).is_visible is True


def test_later_booking_of_the_same_call_replaces_the_earlier_time():
    factory, (mailbox_id, _) = _database()
    earlier_ics = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
DTSTART;TZID=America/Chicago:20260924T150000
DTEND;TZID=America/Chicago:20260924T153000
END:VEVENT
END:VCALENDAR
"""
    later_ics = earlier_ics.replace("T150000", "T140000").replace("T153000", "T143000")
    with factory() as db:
        first = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="tunya-3pm",
            thread_id="tunya-3pm",
            subject="Appointment booked: Call with Tunya (Mark Wei) @ Thu Sep 24, 2026 3pm - 3:30pm (CDT)",
            sender="Tunya Irkad <tunya@limrun.com>",
            received_at=datetime(2026, 9, 22, 21, 54, tzinfo=timezone.utc),
            body_text=earlier_ics,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Limrun",
            job_role="Founding Engineer",
        )
        second = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="tunya-2pm",
            thread_id="tunya-2pm",
            subject="Appointment booked: Call with Tunya (Mark Wei) @ Thu Sep 24, 2026 2pm - 2:30pm (CDT)",
            sender="Tunya Irkad <tunya@limrun.com>",
            received_at=datetime(2026, 9, 22, 21, 56, tzinfo=timezone.utc),
            body_text=later_ics,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Limrun",
            job_role="Founding Engineer",
        )
        other = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="hasan",
            thread_id="hasan",
            subject="Appointment booked: 30 min with Hasan (Mark Wei) @ Mon Sep 28, 2026 2pm - 2:30pm (CDT)",
            sender="Hasan <hasan@limrun.com>",
            received_at=datetime(2026, 9, 24, 16, 57, tzinfo=timezone.utc),
            body_text=later_ics.replace("20260924", "20260928"),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            company="Limrun",
            job_role="Founding Engineer",
        )
        db.add_all([first, second, other])
        db.flush()
        three_pm = CalendarEvent(mailbox_id=mailbox_id, source_email_id=first.id, kind="confirmed", origin="email", company="Limrun", job_role="Founding Engineer", interviewer="Tunya Irkad", start_at=datetime(2026, 9, 24, 20, tzinfo=timezone.utc), end_at=datetime(2026, 9, 24, 20, 30, tzinfo=timezone.utc), is_visible=True)
        two_pm = CalendarEvent(mailbox_id=mailbox_id, source_email_id=second.id, kind="confirmed", origin="email", company="Limrun", job_role="Founding Engineer", interviewer="tunya@limrun.com", start_at=datetime(2026, 9, 24, 19, tzinfo=timezone.utc), end_at=datetime(2026, 9, 24, 19, 30, tzinfo=timezone.utc), is_visible=True)
        hasan = CalendarEvent(mailbox_id=mailbox_id, source_email_id=other.id, kind="confirmed", origin="email", company="Limrun", job_role="Founding Engineer", interviewer="Hasan", start_at=datetime(2026, 9, 28, 19, tzinfo=timezone.utc), end_at=datetime(2026, 9, 28, 19, 30, tzinfo=timezone.utc), is_visible=True)
        db.add_all([three_pm, two_pm, hasan])
        db.commit()

        assert collapse_duplicate_calendar_events(db) is True
        db.commit()
        assert db.get(CalendarEvent, three_pm.id).is_visible is False
        assert db.get(CalendarEvent, three_pm.id).superseded_by_event_id == two_pm.id
        assert db.get(CalendarEvent, two_pm.id).is_visible is True
        assert db.get(CalendarEvent, hasan.id).is_visible is True


def test_discarded_pending_interview_leaves_alerts_and_calendar_until_restored():
    factory, (mailbox_id, _) = _database()
    received = datetime.now(timezone.utc) - timedelta(days=1)
    with factory() as db:
        older = EmailMessage(mailbox_id=mailbox_id, provider_message_id="ask-1", thread_id="acme", subject="Interview times", sender="recruiter@acme.test", received_at=received - timedelta(days=2), label=EmailLabel.INTERVIEW_INVITATION.value, company="Acme", job_role="Engineer")
        request = EmailMessage(mailbox_id=mailbox_id, provider_message_id="ask-2", thread_id="acme", subject="Need to reschedule", sender="recruiter@acme.test", received_at=received, label=EmailLabel.INTERVIEW_INVITATION.value, company="Acme", job_role="Engineer")
        db.add_all([older, request])
        db.flush()
        start = datetime.now(timezone.utc) + timedelta(days=3)
        db.add(CalendarEvent(mailbox_id=mailbox_id, source_email_id=request.id, kind="availability", origin="email", title="Available interview", start_at=start, end_at=start + timedelta(minutes=30), meeting_type="unspecified", is_visible=True))
        db.commit()
        request_id = request.id

    client = _client(factory)
    window = {"start": (start - timedelta(days=1)).isoformat(), "end": (start + timedelta(days=1)).isoformat()}
    assert [item["email_id"] for item in client.get("/api/calendar-events/pending").json()] == [request_id]
    assert len(client.get("/api/calendar-events", params=window).json()) == 1

    assert client.post(f"/api/calendar-events/pending/{request_id}/dismiss").status_code == 204
    # The older request for the same interview must not resurface in its place.
    assert client.get("/api/calendar-events/pending").json() == []
    assert client.get("/api/calendar-events", params=window).json() == []
    assert request_id not in [item["email_id"] for item in client.get("/api/emails/interview-alerts").json()]

    assert client.delete(f"/api/calendar-events/pending/{request_id}/dismiss").status_code == 204
    assert [item["email_id"] for item in client.get("/api/calendar-events/pending").json()] == [request_id]
    assert client.post("/api/calendar-events/pending/999999/dismiss").status_code == 404


def test_interview_stage_labels_are_not_job_roles():
    assert is_interview_stage_label("Recruiter Screen")
    assert is_interview_stage_label("Phone Screen")
    assert is_interview_stage_label("Technical Interview")
    assert is_interview_stage_label("Final Round")
    assert not is_interview_stage_label("Staff Software Engineer")
    assert not is_interview_stage_label("Senior Recruiter")
    assert job_role_or_empty("Recruiter Screen") == ""
    assert job_role_or_empty("Staff/Senior Staff Software Engineer, Product Engineering") == (
        "Staff/Senior Staff Software Engineer, Product Engineering"
    )


def test_repair_replaces_recruiter_screen_with_real_job_title():
    factory, (mailbox_id, _) = _database()
    received = datetime(2026, 10, 5, 13, 27, tzinfo=timezone.utc)
    start = datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc)
    with factory() as db:
        role_email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="harvey-role",
            thread_id="harvey-thread-a",
            subject="Staff/Senior Staff Software Engineer, Product Engineering",
            sender="Patricia Mafioletti <patricia@harvey.ai>",
            received_at=received - timedelta(hours=1),
            label=EmailLabel.APPLICATION_CONFIRMATION.value,
            company="Harvey",
            job_role="Staff/Senior Staff Software Engineer, Product Engineering",
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="harvey-invite",
            thread_id="harvey-thread-b",
            subject="Invitation from an unknown calendar system",
            sender="Patricia Mafioletti <patricia@harvey.ai>",
            received_at=received,
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            company="Harvey",
            job_role="Recruiter Screen",
        )
        db.add_all([role_email, invite])
        db.flush()
        event = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=invite.id,
            kind="confirmed",
            origin="email",
            title="Harvey — Recruiter Screen",
            company="Harvey",
            job_role="Recruiter Screen",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            interview_status="calendar_invite",
            application_status="active",
            source_received_at=received,
            is_visible=True,
        )
        db.add(event)
        db.commit()
        invite_id = invite.id
        event_id = event.id

        assert repair_interview_stage_roles(db) >= 1
        db.commit()
        invite = db.get(EmailMessage, invite_id)
        event = db.get(CalendarEvent, event_id)
        assert invite.job_role == "Staff/Senior Staff Software Engineer, Product Engineering"
        assert event.job_role == "Staff/Senior Staff Software Engineer, Product Engineering"
        assert "Recruiter Screen" not in (event.title or "")


def test_weekday_confirmation_uses_the_next_matching_day():
    received = datetime(2026, 10, 1, 13, 57, tzinfo=timezone.utc)
    slots = parse_text_slots(
        "Thanks for confirming. I look forward to speaking with you on Monday at 2:00 PM ET.",
        received,
    )
    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 5, 18, 30, tzinfo=timezone.utc)


def test_shared_trailing_meridiem_is_an_availability_window():
    received = datetime(2026, 10, 5, tzinfo=timezone.utc)
    slots = parse_text_slots(
        "October 8, anytime between 9:00 and 11:00 a.m. Pacific Time, works well for me.",
        received,
    )
    assert len(slots) == 1
    assert slots[0].is_availability_window is True
    assert slots[0].start_at == datetime(2026, 10, 8, 16, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 8, 18, tzinfo=timezone.utc)


def test_zone_stated_before_a_list_applies_to_each_offered_time():
    received = datetime(2026, 10, 1, 13, 45, tzinfo=timezone.utc)
    slots = parse_text_slots(
        "I'm available at any of these times, all in Eastern Time:\n"
        "- October 2 at 12:00 PM\n"
        "- October 5 at 2:00 PM\n"
        "- October 6 at 10:00 AM\n"
        "On Oct 1, 2026, 9:24 AM -0400, Emily wrote:",
        received,
    )
    assert sorted((slot.start_at, slot.end_at) for slot in slots) == [
        (datetime(2026, 10, 2, 16, tzinfo=timezone.utc), datetime(2026, 10, 2, 16, 30, tzinfo=timezone.utc)),
        (datetime(2026, 10, 5, 18, tzinfo=timezone.utc), datetime(2026, 10, 5, 18, 30, tzinfo=timezone.utc)),
        (datetime(2026, 10, 6, 14, tzinfo=timezone.utc), datetime(2026, 10, 6, 14, 30, tzinfo=timezone.utc)),
    ]


def test_ranged_call_length_uses_the_longer_bound():
    received = datetime(2026, 10, 6, 15, tzinfo=timezone.utc)
    slots = parse_text_slots(
        "Would you be available today at 9am time in Tampa for a brief call (10-15 minutes)?",
        received,
    )
    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 6, 13, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 6, 13, 15, tzinfo=timezone.utc)
    assert slots[0].end_is_explicit is True


def test_quote_clock_availability_is_replaced_by_the_offered_window():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="springs-offer",
            thread_id="springs-thread",
            subject="Re: Springs Window Fashions - Interview Availability",
            sender="Jordan <jordan@example.com>",
            received_at=datetime(2026, 10, 5, 11, 59, tzinfo=timezone.utc),
            body_text=(
                "October 8 between 11:00 a.m. and 1:00 p.m. Pacific Time works well for a brief call.\n"
                "On Oct 1, 2026, 11:46 AM -0400, Theresa wrote:"
            ),
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="Springs Window Fashions",
            job_role="Senior AI Engineer",
        )
        db.add(email)
        db.flush()
        phantom = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=email.id,
            kind="availability",
            origin="email",
            slot_index=0,
            title="Springs Window Fashions — Senior AI Engineer",
            company="Springs Window Fashions",
            job_role="Senior AI Engineer",
            start_at=datetime(2026, 10, 1, 15, 46, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 1, 16, 16, tzinfo=timezone.utc),
            source_timezone="America/Los_Angeles",
            application_status="active",
            is_visible=True,
        )
        db.add(phantom)
        db.commit()
        event_id = phantom.id

        assert repair_phantom_availability_slots(db, mailbox_id) >= 1
        db.commit()
        event = db.get(CalendarEvent, event_id)
        assert event.is_visible is True
        assert event.start_at == datetime(2026, 10, 8, 18, tzinfo=timezone.utc)
        assert event.end_at == datetime(2026, 10, 8, 20, tzinfo=timezone.utc)


def test_phantom_repair_does_not_append_a_duplicate_when_company_is_missing():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="companyless-offer",
            thread_id="companyless-thread",
            subject="",
            sender="candidate@example.com",
            received_at=datetime(2026, 10, 11, 14, tzinfo=timezone.utc),
            body_text="October 13 at 10:00 a.m. Eastern works well for a phone call.",
            label=EmailLabel.INTERVIEW_AVAILABILITY.value,
            company="",
            job_role="Staff Machine Learning Engineer",
        )
        db.add(email)
        db.flush()
        db.add(CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=email.id,
            source_thread_id=email.thread_id,
            slot_index=0,
            kind="availability",
            origin="email",
            title=email.job_role,
            company="",
            job_role=email.job_role,
            start_at=datetime(2026, 10, 13, 14, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 13, 14, 30, tzinfo=timezone.utc),
            is_visible=True,
        ))
        db.commit()

        repair_phantom_availability_slots(db, mailbox_id)
        db.commit()

        rows = db.query(CalendarEvent).filter(CalendarEvent.source_email_id == email.id).all()
        assert len(rows) == 1
        assert rows[0].is_visible is True


def test_same_source_companyless_duplicate_chips_collapse():
    factory, (mailbox_id, _) = _database()
    start = datetime(2026, 10, 13, 14, tzinfo=timezone.utc)
    with factory() as db:
        email = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="duplicate-companyless-offer",
            thread_id="duplicate-companyless-thread",
            subject="",
            sender="candidate@example.com",
            received_at=datetime(2026, 10, 11, 14, tzinfo=timezone.utc),
            body_text="October 13 at 10:00 a.m. Eastern works well.",
            label=EmailLabel.INTERVIEW_AVAILABILITY.value,
            company="",
            job_role="Staff Machine Learning Engineer",
        )
        db.add(email)
        db.flush()
        db.add_all([
            CalendarEvent(mailbox_id=mailbox_id, source_email_id=email.id, source_thread_id=email.thread_id, slot_index=0, kind="availability", origin="email", title=email.job_role, company="", job_role=email.job_role, start_at=start, end_at=start + timedelta(minutes=30), is_visible=True),
            CalendarEvent(mailbox_id=mailbox_id, source_email_id=email.id, source_thread_id=email.thread_id, slot_index=1, kind="availability", origin="email", title=email.job_role, company="", job_role=email.job_role, start_at=start, end_at=start + timedelta(minutes=30), is_visible=True),
        ])
        db.commit()

        assert collapse_duplicate_calendar_events(db, mailbox_id) is True
        db.commit()

        visible = db.query(CalendarEvent).filter(
            CalendarEvent.source_email_id == email.id,
            CalendarEvent.is_visible.is_(True),
        ).all()
        assert len(visible) == 1


def test_bare_pacific_zone_is_the_accepted_time_not_a_quoted_option():
    received = datetime(2026, 10, 8, 16, 12, tzinfo=timezone.utc)
    email = SimpleNamespace(
        subject="Re: ADUSA Application",
        snippet="October 13 at 10:00 am Pacific works well for me.",
        body_text=(
            "October 13 at 10:00 a.m. Pacific works well for me.\n"
            "On Oct 8, 2026, 12:06 PM -0400, Chase Wiggs wrote:\n"
            "October 12th at 8am or 9am PST\n"
            "October 13th at 9am, 9:30am, or 10am PST\n"
        ),
        body_html="",
        received_at=received,
    )
    slots = message_appointment_slots(email)
    assert len(slots) == 1
    assert slots[0].start_at == datetime(2026, 10, 13, 17, tzinfo=timezone.utc)
    assert slots[0].end_at == datetime(2026, 10, 13, 17, 30, tzinfo=timezone.utc)


def test_quoted_option_booking_yields_to_the_later_invite():
    factory, (mailbox_id, _) = _database()
    with factory() as db:
        reply = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="adusa-reply",
            thread_id="adusa-thread",
            subject="Re: ADUSA Application",
            sender="mark@example.com",
            received_at=datetime(2026, 10, 8, 16, 12, tzinfo=timezone.utc),
            body_text=(
                "October 13 at 10:00 a.m. Pacific works well for me.\n"
                "On Oct 8, 2026, 12:06 PM -0400, Chase Wiggs wrote:\n"
                "October 12th at 8am PST\n"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            company="ADUSA",
            job_role="Platform Administrator IV",
        )
        invite = EmailMessage(
            mailbox_id=mailbox_id,
            provider_message_id="adusa-invite",
            thread_id="adusa-thread",
            subject="Principal Platform Engineer Interview",
            sender="Chase Wiggs <chase@adusa.com>",
            received_at=datetime(2026, 10, 8, 16, 13, tzinfo=timezone.utc),
            body_text=(
                "Join: https://teams.microsoft.com/meet/36304861417217?p=\n"
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
                "DTSTART;TZID=America/Chicago:20261013T120000\n"
                "DTEND;TZID=America/Chicago:20261013T123000\n"
                "END:VEVENT\nEND:VCALENDAR\n"
                "Join: https://teams.microsoft.com/meet/36304861417217?p=VjgU38s8zKm2tDgwbq\n"
            ),
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            company="adusa",
            job_role="Principal Platform Engineer",
        )
        db.add_all([reply, invite])
        db.flush()
        wrong = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=reply.id,
            kind="confirmed",
            origin="email",
            title="ADUSA — Platform Administrator IV",
            company="ADUSA",
            job_role="Platform Administrator IV",
            start_at=datetime(2026, 10, 12, 15, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 12, 16, tzinfo=timezone.utc),
            interview_status="confirmation",
            application_status="active",
            meeting_type="phone",
            source_received_at=reply.received_at,
            is_visible=True,
        )
        booked = CalendarEvent(
            mailbox_id=mailbox_id,
            source_email_id=invite.id,
            kind="confirmed",
            origin="email",
            title="adusa — Principal Platform Engineer",
            company="adusa",
            job_role="Principal Platform Engineer",
            start_at=datetime(2026, 10, 13, 17, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 13, 17, 30, tzinfo=timezone.utc),
            interview_status="calendar_invite",
            application_status="active",
            meeting_type="video",
            meeting_provider="Microsoft Teams",
            meeting_url="https://teams.microsoft.com/meet/36304861417217?p=",
            source_received_at=invite.received_at,
            is_visible=True,
        )
        db.add_all([wrong, booked])
        db.commit()
        wrong_id, booked_id = wrong.id, booked.id

        assert repair_quoted_option_bookings(db, mailbox_id) == 1
        assert repair_meeting_join_urls(db, mailbox_id) == 1
        db.commit()
        wrong = db.get(CalendarEvent, wrong_id)
        booked = db.get(CalendarEvent, booked_id)
        assert wrong.is_visible is False
        assert wrong.superseded_by_event_id == booked.id
        assert booked.is_visible is True
        assert booked.company == "ADUSA"
        assert booked.job_role == "Principal Platform Engineer"
        assert booked.meeting_url == "https://teams.microsoft.com/meet/36304861417217?p=VjgU38s8zKm2tDgwbq"
