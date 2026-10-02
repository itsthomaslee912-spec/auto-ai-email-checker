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
