from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import emails
from app.db import Base, get_db
from app.models import EmailLabel, EmailMessage, MailboxConnection, Provider, User


def _client_with_email() -> tuple[TestClient, int]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with session_factory() as db:
        user = User(external_id="u-ai-reply")
        db.add(user)
        db.flush()
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.GOOGLE.value,
            email_address="owner@example.com",
            access_token_enc="x",
            refresh_token_enc="",
            is_active=True,
        )
        db.add(mailbox)
        db.flush()
        message = EmailMessage(
            mailbox_id=mailbox.id,
            provider_message_id="reply-context-1",
            subject="Interview availability",
            sender="Recruiter <recruiter@example.com>",
            snippet="Are you available Tuesday?",
            body_text="Are you available for an interview next Tuesday at 2 PM?",
            label=EmailLabel.INTERVIEW_INVITATION.value,
        )
        db.add(message)
        db.commit()
        db.refresh(message)
        email_id = message.id

    app = FastAPI()
    app.include_router(emails.router)

    def override_get_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), email_id


def test_ai_reply_uses_description_and_original_email(monkeypatch):
    client, email_id = _client_with_email()
    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Tuesday at 2 PM works for me."))]
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(
        emails,
        "get_ai_backend",
        lambda: SimpleNamespace(api_key="test-key", model="test-model", client=lambda: fake_client),
    )

    response = client.post(
        f"/api/emails/{email_id}/ai-reply",
        json={"instruction": "Accept warmly and keep the reply short."},
    )

    assert response.status_code == 200
    assert response.json() == {"body_text": "Tuesday at 2 PM works for me."}
    messages = captured["messages"]
    assert "Accept warmly and keep the reply short." in messages[1]["content"]
    assert "Interview availability" in messages[1]["content"]
    assert "Are you available for an interview next Tuesday at 2 PM?" in messages[1]["content"]

