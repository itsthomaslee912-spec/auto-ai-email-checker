"""Count invitation emails the new parser would repair. No message text."""
from app.db import SessionLocal
from app.models import CalendarEvent, EmailLabel, EmailMessage
from app.services.calendar_extract import _calendar_text, parse_ics_slots, parse_text_slots
from app.timeutil import as_utc

db = SessionLocal()
emails = (
    db.query(EmailMessage)
    .filter(EmailMessage.label == EmailLabel.INTERVIEW_INVITATION.value)
    .all()
)
extra = 0
for email in emails:
    text = _calendar_text(email)
    slots = parse_ics_slots(text) or parse_text_slots(text, email.received_at)
    if not slots:
        continue
    rows = (
        db.query(CalendarEvent.start_at)
        .filter(
            CalendarEvent.source_email_id == email.id,
            CalendarEvent.kind == "availability",
        )
        .all()
    )
    stored = {as_utc(row[0]) for row in rows}
    if not {slot.start_at for slot in slots}.issubset(stored):
        extra += 1
print(f"invitations={len(emails)} would_repair={extra}")
db.close()
