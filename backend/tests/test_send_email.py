from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import emails
from app.db import Base
from app.models import CalendarEvent, EmailLabel, EmailMessage, MailboxConnection, Provider, User
from app.schemas import SendEmailIn


def test_reply_ui_classifies_and_projects_outgoing_time_acceptance():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with factory() as db:
        user = User(external_id="send-email-user")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.MICROSOFT.value,
            email_address="tlee941212@outlook.com",
            display_name="Thomas",
            access_token_enc="x",
            refresh_token_enc="",
            is_active=True,
        )
        db.add(mailbox)
        db.flush()
        proposal = EmailMessage(
            mailbox_id=mailbox.id,
            provider_message_id="autozone-seven-am-proposal",
            thread_id="autozone-thread",
            folder="inbox",
            subject="RE: AutoZone",
            sender="shipra.khandelwal@autozone.com",
            recipients=mailbox.email_address,
            received_at=datetime(2026, 11, 4, 23, 10, tzinfo=UTC),
            body_text=(
                "The interview will need to be scheduled for 7:00 AM CST "
                "on November 10 instead of 8:00 AM. Would that still work for you?"
            ),
            label=EmailLabel.INTERVIEW_INVITATION.value,
            company="AutoZone",
            job_role="Systems Engineer",
        )
        db.add(proposal)
        db.commit()

        payload = SendEmailIn(
            mailbox_id=mailbox.id,
            to_address="shipra.khandelwal@autozone.com",
            subject="RE: AutoZone",
            body_text="7:00 AM CST on November 10 works for me.",
            reply_to_email_id=proposal.id,
        )
        background = BackgroundTasks()
        with (
            patch.object(emails, "ensure_microsoft_access_token", AsyncMock(return_value="token")),
            patch.object(emails, "outlook_reply_message", AsyncMock()),
            patch.object(emails, "collapse_duplicate_calendar_events_post_commit", AsyncMock(return_value=False)) as collapse,
        ):
            result = asyncio.run(emails.send_email(payload, background, db))

        assert result.ok is True
        sent = db.query(EmailMessage).filter(EmailMessage.folder == "sent").one()
        assert sent.label == EmailLabel.INTERVIEW_SCHEDULED.value
        assert sent.interview_subtype == "confirmation"
        assert sent.classification_pending is False
        assert sent.company == "AutoZone"
        assert sent.job_role == "Systems Engineer"
        event = db.query(CalendarEvent).filter(CalendarEvent.source_email_id == sent.id).one()
        assert event.kind == "confirmed"
        assert event.company == "AutoZone"
        assert event.job_role == "Systems Engineer"
        assert event.start_at == datetime(2026, 11, 10, 13, 0, tzinfo=UTC)
        assert len(background.tasks) == 1
        assert background.tasks[0].func is collapse

