from __future__ import annotations

import asyncio
import base64

from app.email.gmail import _hydrate_calendar_attachments, normalize_gmail_message


class _Response:
    status_code = 200

    def __init__(self, data: str):
        self._data = data

    def json(self) -> dict:
        return {"data": self._data}


class _Client:
    def __init__(self, data: str):
        self.data = data
        self.urls: list[str] = []

    async def get(self, url: str, **_kwargs) -> _Response:
        self.urls.append(url)
        return _Response(self.data)


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_calendar_attachment_is_hydrated_and_included_in_normalized_text():
    ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261002T173000Z
DTEND:20261002T180000Z
END:VEVENT
END:VCALENDAR"""
    raw = {
        "id": "message-1",
        "threadId": "thread-1",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "Interview confirmed"},
                {"name": "From", "value": "Recruiter <recruiter@example.com>"},
                {"name": "Date", "value": "Wed, 30 Sep 2026 00:40:59 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encoded("Your interview is confirmed.")},
                },
                {
                    "mimeType": "text/calendar",
                    "filename": "invite.ics",
                    "body": {"attachmentId": "attachment-1", "size": len(ics)},
                },
            ],
        },
    }
    client = _Client(_encoded(ics))

    asyncio.run(_hydrate_calendar_attachments(client, "token", raw["id"], raw["payload"]))
    normalized = normalize_gmail_message(raw)

    assert client.urls == [
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/message-1/attachments/attachment-1"
    ]
    assert "BEGIN:VCALENDAR" in normalized["body_text"]
    assert "DTSTART:20261002T173000Z" in normalized["body_text"]


def test_bookings_application_ics_attachment_is_hydrated_and_kept():
    """Microsoft Bookings mails the appointment as application/ics, not text/calendar."""
    ics = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
SUMMARY:Mark Wei - Exploratory Call
DTSTART;TZID=Pacific Standard Time:20261001T100000
DTEND;TZID=Pacific Standard Time:20261001T103000
END:VEVENT
END:VCALENDAR"""
    raw = {
        "id": "bookings-1",
        "threadId": "bookings-thread",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Mark Wei - Exploratory Call"},
                {"name": "From", "value": "\"Cheyane Reisner (cw)\" <creisner@nvidia.com>"},
                {"name": "Date", "value": "Mon, 28 Sep 2026 20:45:33 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": _encoded("<p>This meeting was scheduled from the bookings page.</p>")},
                },
                {
                    "mimeType": "application/ics",
                    "filename": "invite.ics",
                    "body": {"attachmentId": "bookings-attachment", "size": len(ics)},
                },
            ],
        },
    }
    client = _Client(_encoded(ics))

    asyncio.run(_hydrate_calendar_attachments(client, "token", raw["id"], raw["payload"]))
    normalized = normalize_gmail_message(raw)

    assert client.urls == [
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/bookings-1/attachments/bookings-attachment"
    ]
    assert "DTSTART;TZID=Pacific Standard Time:20261001T100000" in normalized["body_text"]


def test_calendar_time_survives_angle_bracket_links_in_the_description():
    """Teams ICS files contain <https://...> after the prose and before DTSTART."""
    ics = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
DESCRIPTION:Need help?<https://aka.ms/JoinTeamsMeeting>
DTSTART;TZID=Pacific Standard Time:20260929T100000
DTEND;TZID=Pacific Standard Time:20260929T104500
SUMMARY:Interview with Extreme Networks
END:VEVENT
END:VCALENDAR"""
    raw = {
        "id": "message-2",
        "threadId": "thread-2",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "Interview with Extreme Networks"},
                {"name": "From", "value": "Yana <ysylvester@extremenetworks.com>"},
                {"name": "Date", "value": "Tue, 22 Sep 2026 23:10:29 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encoded("Time: 10:00 AM PDT\nDuration: Up to 45 Minutes\nNeed help?")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _encoded("<p>Time: 10:00 AM PDT</p>")},
                },
                {
                    "mimeType": "text/calendar",
                    "filename": "invite.ics",
                    "body": {"data": _encoded(ics)},
                },
            ],
        },
    }

    normalized = normalize_gmail_message(raw)

    assert "DTSTART;TZID=Pacific Standard Time:20260929T100000" in normalized["body_text"]
    assert "DTEND;TZID=Pacific Standard Time:20260929T104500" in normalized["body_text"]
    assert "<p>Time: 10:00 AM PDT</p>" in normalized["body_html"]
