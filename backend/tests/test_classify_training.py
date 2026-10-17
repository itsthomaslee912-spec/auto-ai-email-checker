from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import classify, emails
from app.classify.openai_classifier import classify_email
from app.classify.correction_retrieval import load_correction_corpus, retrieve_relevant_corrections
from app.classify.prompt import CLASSIFY_TYPES, SYSTEM_PROMPT
from app.classify.prompt_store import activate_prompt_text, get_active_system_prompt, invalidate_prompt_cache
from app.db import Base, get_db
from app.models import (
    ClassifyCorrection,
    ClassifyPromptVersion,
    CalendarEvent,
    EmailLabel,
    EmailMessage,
    MailboxConnection,
    Provider,
    User,
)
from app.services.prompt_update import PromptUpdateError, _latest_corrections, update_prompt_from_corrections


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _seed_email(db, *, label: str = EmailLabel.APPLICATION_CONFIRMATION.value, human_corrected: bool = False) -> EmailMessage:
    user = User(external_id="test-user")
    db.add(user)
    db.flush()
    mailbox = MailboxConnection(
        user_id=user.id,
        provider=Provider.GOOGLE.value,
        email_address="me@example.com",
        access_token_enc="x",
        refresh_token_enc="",
        is_active=True,
    )
    db.add(mailbox)
    db.flush()
    email = EmailMessage(
        mailbox_id=mailbox.id,
        provider_message_id="msg-1",
        subject="Thanks for applying",
        sender="Talent <jobs@cloudbeds.com>",
        snippet="we will not be moving forward",
        body_text="Unfortunately we will not be moving forward with your application.",
        label=label,
        human_corrected=human_corrected,
    )
    db.add(email)
    db.commit()
    db.refresh(email)
    return email


def _client(SessionLocal):
    app = FastAPI()
    app.include_router(emails.router)
    app.include_router(classify.router)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_patch_label_saves_training_and_pins():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db)
    email_id = email.id
    db.close()

    client = _client(SessionLocal)
    with patch(
        "app.classify.outcome_extract.extract_company_role",
        new=AsyncMock(return_value=None),
    ):
        res = client.patch(
            f"/api/emails/{email_id}/label",
            json={"label": "rejected_closed", "save_training": True},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["label"] == "rejected_closed"
    assert body["human_corrected"] is True

    db = SessionLocal()
    stored = db.query(EmailMessage).filter(EmailMessage.id == email_id).one()
    assert stored.label == EmailLabel.REJECTED_CLOSED.value
    assert stored.human_corrected is True
    row = db.query(ClassifyCorrection).one()
    assert row.previous_label == EmailLabel.APPLICATION_CONFIRMATION.value
    assert row.corrected_label == EmailLabel.REJECTED_CLOSED.value
    assert "not be moving forward" in row.body_text
    db.close()


def test_patch_label_without_training_skips_correction():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db, label=EmailLabel.OTHER.value)
    email_id = email.id
    db.close()

    client = _client(SessionLocal)
    res = client.patch(
        f"/api/emails/{email_id}/label",
        json={"label": "recruitment_alert", "save_training": False},
    )
    assert res.status_code == 200, res.text
    db = SessionLocal()
    assert db.query(ClassifyCorrection).count() == 0
    assert db.query(EmailMessage).one().human_corrected is True
    db.close()


def test_outcome_to_outcome_label_change_reuses_existing_extraction():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db, label=EmailLabel.INTERVIEW_INVITATION.value)
    email.company = "Acme"
    email.job_role = "Software Engineer"
    email.outcome_extracted = True
    email.classification_pending = True
    db.commit()
    email_id = email.id
    db.close()

    client = _client(SessionLocal)
    with patch("app.api.emails.apply_outcome", new=AsyncMock()) as extract:
        response = client.patch(
            f"/api/emails/{email_id}/label",
            json={"label": "screening", "save_training": True},
        )

    assert response.status_code == 200, response.text
    extract.assert_not_awaited()
    with SessionLocal() as db:
        stored = db.get(EmailMessage, email_id)
        assert stored.label == EmailLabel.SCREENING.value
        assert stored.classification_pending is False
        assert stored.outcome_extracted is True
        assert stored.company == "Acme"
        assert stored.job_role == "Software Engineer"


def test_scheduled_interview_subtype_is_metadata():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db, label=EmailLabel.OTHER.value)
    email.subject = "Reminder: Interview tomorrow at 2 PM"
    email.body_text = "Your interview is tomorrow."
    db.commit()
    email_id = email.id
    db.close()

    client = _client(SessionLocal)
    with patch("app.classify.outcome_extract.extract_company_role", new=AsyncMock(return_value=None)):
        response = client.patch(
            f"/api/emails/{email_id}/label",
            json={"label": "interview_scheduled", "save_training": False},
        )
    assert response.status_code == 200, response.text
    assert response.json()["label"] == "interview_scheduled"
    assert response.json()["interview_subtype"] == "reminder"

    with patch("app.classify.outcome_extract.extract_company_role", new=AsyncMock(return_value=None)):
        response = client.patch(
            f"/api/emails/{email_id}/label",
            json={"label": "interview_follow_up", "save_training": False},
        )
    assert response.status_code == 200, response.text
    assert response.json()["interview_subtype"] is None


def test_manual_interview_subtype_overrides_inference_and_can_be_changed():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db, label=EmailLabel.OTHER.value)
    email.subject = "Reminder: Interview tomorrow at 2 PM"
    db.commit()
    email_id = email.id
    db.close()

    client = _client(SessionLocal)
    with patch("app.classify.outcome_extract.extract_company_role", new=AsyncMock(return_value=None)):
        response = client.patch(
            f"/api/emails/{email_id}/label",
            json={"label": "interview_scheduled", "interview_subtype": "confirmation", "save_training": False},
        )
    assert response.status_code == 200, response.text
    assert response.json()["interview_subtype"] == "confirmation"

    response = client.patch(
        f"/api/emails/{email_id}/label",
        json={"label": "interview_scheduled", "interview_subtype": "reschedule", "save_training": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["interview_subtype"] == "reschedule"
    db = SessionLocal()
    correction = db.query(ClassifyCorrection).one()
    assert correction.previous_label == correction.corrected_label == "interview_scheduled"
    assert correction.previous_subtype == "confirmation"
    assert correction.corrected_subtype == "reschedule"
    db.close()


def test_prompt_update_receives_subtype_correction(monkeypatch):
    monkeypatch.setattr("app.services.prompt_update.has_saved_ai_settings", lambda: False)
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db, label=EmailLabel.INTERVIEW_SCHEDULED.value)
    db.add(ClassifyCorrection(
        email_id=email.id,
        previous_label="interview_scheduled",
        corrected_label="interview_scheduled",
        previous_subtype="reminder",
        corrected_subtype="confirmation",
        subject=email.subject,
        sender=email.sender,
        snippet=email.snippet,
        body_text=email.body_text,
    ))
    db.commit()
    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured["content"] = kwargs["messages"][1]["content"]
            return SimpleNamespace(id="resp_subtype", choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"prompt": SYSTEM_PROMPT})))])

    monkeypatch.setattr("app.services.prompt_update.get_settings", lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="gpt-4o-mini"))
    monkeypatch.setattr("app.services.prompt_update.AsyncOpenAI", lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))
    result = asyncio.run(update_prompt_from_corrections(db))
    assert result.ok is True
    assert "Previous interview subtype: reminder" in captured["content"]
    assert "Correct interview subtype: confirmation" in captured["content"]
    db.close()
    invalidate_prompt_cache()


def test_training_uses_only_latest_correction_for_each_email():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db)
    db.add_all([
        ClassifyCorrection(
            email_id=email.id,
            previous_label=EmailLabel.RECRUITMENT_ALERT.value,
            corrected_label=EmailLabel.OTHER.value,
            subject="Same message",
        ),
        ClassifyCorrection(
            email_id=email.id,
            previous_label=EmailLabel.OTHER.value,
            corrected_label=EmailLabel.RECRUITMENT_ALERT.value,
            subject="Same message",
        ),
    ])
    db.commit()

    rows = _latest_corrections(db)
    assert len(rows) == 1
    assert rows[0].corrected_label == EmailLabel.RECRUITMENT_ALERT.value
    db.close()


def test_retrieval_excludes_holdout_and_finds_similar_corrections():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db)
    other = EmailMessage(
        mailbox_id=email.mailbox_id,
        provider_message_id="assessment-copy",
        subject="Complete your coding challenge",
        sender="Assessments <noreply@codes.test>",
        snippet="Please complete the coding challenge by Friday.",
        body_text="Please complete the coding challenge by Friday.",
        label=EmailLabel.ASSESSMENT.value,
    )
    db.add(other)
    db.flush()
    db.add_all([
        ClassifyCorrection(
            email_id=email.id,
            previous_label="other",
            corrected_label="rejected_closed",
            subject=email.subject,
            body_text=email.body_text,
            evaluation_holdout=True,
        ),
        ClassifyCorrection(
            email_id=other.id,
            previous_label="screening",
            corrected_label="assessment",
            subject=other.subject,
            sender=other.sender,
            snippet=other.snippet,
            body_text=other.body_text,
            evaluation_holdout=False,
        ),
    ])
    db.commit()

    corpus = load_correction_corpus(db)
    found = retrieve_relevant_corrections(
        corpus,
        subject="Reminder: complete your coding challenge",
        sender="noreply@codes.test",
        body_text="Your coding challenge is due Friday.",
        snippet="",
    )
    assert [item.corrected_label for item in found] == ["assessment"]
    assert all(item.corrected_label != "rejected_closed" for item in corpus)
    db.close()


def test_training_deduplicates_conflicting_copies_of_same_template():
    SessionLocal = _session_factory()
    db = SessionLocal()
    first = _seed_email(db)
    second = EmailMessage(
        mailbox_id=first.mailbox_id,
        provider_message_id="msg-template-copy",
        subject="Thank you for applying",
        sender="Talent <jobs@example.com>",
        snippet="Dear Ryan, Thank you for applying. Unfortunately, we cannot proceed.",
        body_text="Dear Ryan, Thank you for applying. Unfortunately, we cannot proceed with your application.",
        label=EmailLabel.REJECTED_CLOSED.value,
    )
    db.add(second)
    db.flush()
    db.add_all([
        ClassifyCorrection(
            email_id=first.id,
            previous_label=EmailLabel.REJECTED_CLOSED.value,
            corrected_label=EmailLabel.APPLICATION_CONFIRMATION.value,
            subject="Thank you for applying",
            snippet="Dear Jordan, Thank you for applying. Unfortunately, we cannot proceed.",
            body_text="Dear Jordan, Thank you for applying. Unfortunately, we cannot proceed with your application.",
        ),
        ClassifyCorrection(
            email_id=second.id,
            previous_label=EmailLabel.APPLICATION_CONFIRMATION.value,
            corrected_label=EmailLabel.REJECTED_CLOSED.value,
            subject="Thank you for applying",
            snippet=second.snippet,
            body_text=second.body_text,
        ),
    ])
    db.commit()

    rows = _latest_corrections(db)
    assert len(rows) == 1
    assert rows[0].email_id == second.id
    assert rows[0].corrected_label == EmailLabel.REJECTED_CLOSED.value
    db.close()


def test_reclassify_query_skips_human_corrected():
    SessionLocal = _session_factory()
    db = SessionLocal()
    locked = _seed_email(db, human_corrected=True)
    unlocked = EmailMessage(
        mailbox_id=locked.mailbox_id,
        provider_message_id="msg-2",
        subject="Interview confirmed",
        sender="Kate",
        snippet="You are confirmed",
        body_text="You are confirmed for your interview.",
        label=EmailLabel.OTHER.value,
        human_corrected=False,
    )
    db.add(unlocked)
    db.commit()
    rows = (
        db.query(EmailMessage.id)
        .filter(
            EmailMessage.mailbox_id == locked.mailbox_id,
            EmailMessage.human_corrected.is_not(True),
        )
        .all()
    )
    ids = {row[0] for row in rows}
    assert unlocked.id in ids
    assert locked.id not in ids
    db.close()


def test_reclassify_commits_calendar_changes_before_worker_session_closes(monkeypatch):
    from app.services import reclassify

    SessionLocal = _session_factory()
    with SessionLocal() as db:
        email = _seed_email(db, label=EmailLabel.OTHER.value)
        email.subject = "Interview confirmed for October 20 at 2 PM"
        email.body_text = "Your interview is confirmed for October 20 at 2 PM."
        db.commit()
        mailbox_id = email.mailbox_id

    async def classify_as_interview(**_kwargs):
        return SimpleNamespace(
            label=EmailLabel.INTERVIEW_SCHEDULED.value,
            interview_subtype="confirmation",
            confidence=1.0,
            response_id="test-response",
        )

    async def create_calendar_event(db, email, *, force=False):
        start = datetime(2026, 10, 20, 18, tzinfo=timezone.utc)
        db.add(CalendarEvent(
            mailbox_id=email.mailbox_id,
            source_email_id=email.id,
            kind="confirmed",
            origin="email",
            title="Interview",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            is_visible=True,
        ))
        return True

    monkeypatch.setattr("app.db.SessionLocal", SessionLocal)
    monkeypatch.setattr(reclassify, "classify_email", classify_as_interview)
    monkeypatch.setattr(reclassify, "process_calendar_email", create_calendar_event)
    monkeypatch.setattr(reclassify, "get_ai_backend", lambda: SimpleNamespace(provider="test"))
    monkeypatch.setattr(reclassify, "publish", AsyncMock())
    reclassify._ai_gate = None
    reclassify._mailbox_gate = None

    asyncio.run(reclassify._run_reclassify_job(mailbox_id, pending_only=False))

    with SessionLocal() as db:
        assert db.query(CalendarEvent).filter(CalendarEvent.mailbox_id == mailbox_id).count() == 1


def test_prompt_loader_falls_back_to_seed(monkeypatch):
    invalidate_prompt_cache()
    monkeypatch.setattr("app.classify.prompt_store._load_from_db", lambda: None)
    assert get_active_system_prompt() == SYSTEM_PROMPT
    invalidate_prompt_cache()


def test_update_prompt_from_corrections(monkeypatch):
    monkeypatch.setattr("app.services.prompt_update.has_saved_ai_settings", lambda: False)
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db)
    db.add(
        ClassifyCorrection(
            email_id=email.id,
            previous_label=EmailLabel.APPLICATION_CONFIRMATION.value,
            corrected_label=EmailLabel.REJECTED_CLOSED.value,
            subject=email.subject,
            sender=email.sender,
            snippet=email.snippet,
            body_text=email.body_text,
        )
    )
    db.commit()

    updated = SYSTEM_PROMPT + "\n- Cloudbeds-style polite turndowns are rejected, not applied.\n"

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                id="resp_prompt",
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"prompt": updated})))],
            )

    monkeypatch.setattr(
        "app.services.prompt_update.get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        "app.services.prompt_update.AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )

    result = asyncio.run(update_prompt_from_corrections(db))
    assert result.ok is True
    assert result.example_count == 1
    active = (
        db.query(ClassifyPromptVersion)
        .filter(ClassifyPromptVersion.is_active.is_(True))
        .one()
    )
    assert "Cloudbeds-style" in active.prompt_text
    assert db.query(ClassifyCorrection).one().used_in_prompt_version_id == active.id
    assert get_active_system_prompt() == active.prompt_text
    db.close()
    invalidate_prompt_cache()


def test_update_prompt_requires_examples(monkeypatch):
    SessionLocal = _session_factory()
    db = SessionLocal()
    monkeypatch.setattr(
        "app.services.prompt_update.get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="gpt-4o-mini"),
    )
    try:
        asyncio.run(update_prompt_from_corrections(db))
        assert False, "expected PromptUpdateError"
    except PromptUpdateError as exc:
        assert "No unused training examples" in str(exc)
    db.close()


def test_prompt_status_endpoint():
    SessionLocal = _session_factory()
    client = _client(SessionLocal)
    res = client.get("/api/classify/prompt")
    assert res.status_code == 200, res.text
    assert res.json()["unused_count"] == 0


def test_evaluation_endpoint_reports_only_held_out_confusion():
    SessionLocal = _session_factory()
    with SessionLocal() as db:
        email = _seed_email(db)
        db.add_all([
            ClassifyCorrection(
                email_id=email.id,
                previous_label="application_confirmation",
                corrected_label="rejected_closed",
                evaluation_holdout=True,
            ),
            ClassifyCorrection(
                email_id=email.id,
                previous_label="other",
                corrected_label="recruitment_alert",
                evaluation_holdout=False,
            ),
        ])
        db.commit()

    body = _client(SessionLocal).get("/api/classify/evaluation").json()
    assert body["total"] == 1
    assert body["accuracy"] == 0
    assert body["confusion_matrix"] == {
        "rejected_closed": {"application_confirmation": 1},
    }


def test_list_training_examples_marks_used_and_unused():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db)
    unused = ClassifyCorrection(
        email_id=email.id,
        previous_label=EmailLabel.APPLICATION_CONFIRMATION.value,
        corrected_label=EmailLabel.REJECTED_CLOSED.value,
        subject=email.subject,
        sender=email.sender,
        snippet=email.snippet,
        body_text=email.body_text,
    )
    version = ClassifyPromptVersion(
        prompt_text=SYSTEM_PROMPT,
        is_active=True,
        source="seed",
        example_count=1,
    )
    db.add_all([unused, version])
    db.flush()
    used = ClassifyCorrection(
        email_id=email.id,
        previous_label=EmailLabel.OTHER.value,
        corrected_label=EmailLabel.INTERVIEW_SCHEDULED.value,
        subject="Interview next week",
        sender="Kate",
        snippet="confirmed",
        body_text="You are confirmed",
        used_in_prompt_version_id=version.id,
    )
    db.add(used)
    db.commit()
    db.close()

    client = _client(SessionLocal)
    res = client.get("/api/classify/training")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 2
    assert body["unused_count"] == 1
    items = body["items"]
    assert items[0]["id"] > items[1]["id"]
    used_flags = {item["subject"]: item["used_in_prompt_version_id"] is not None for item in items}
    assert used_flags["Thanks for applying"] is False
    assert used_flags["Interview next week"] is True
    assert "body_text" not in items[0]

    newest = client.get("/api/classify/training", params={"limit": 1, "offset": 0})
    assert newest.status_code == 200, newest.text
    newest_body = newest.json()
    assert newest_body["total"] == 2
    assert newest_body["unused_count"] == 1
    assert len(newest_body["items"]) == 1
    assert newest_body["items"][0]["subject"] == "Interview next week"

    older = client.get("/api/classify/training", params={"limit": 1, "offset": 1})
    assert older.status_code == 200, older.text
    assert older.json()["total"] == 2
    assert older.json()["items"][0]["subject"] == "Thanks for applying"


def test_delete_only_unused_training_examples():
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db)
    version = ClassifyPromptVersion(prompt_text=SYSTEM_PROMPT, is_active=True, source="seed", example_count=1)
    db.add(version)
    db.flush()
    rows = [
        ClassifyCorrection(email_id=email.id, previous_label="other", corrected_label="screening", used_in_prompt_version_id=None),
        ClassifyCorrection(email_id=email.id, previous_label="other", corrected_label="assessment", used_in_prompt_version_id=None),
        ClassifyCorrection(email_id=email.id, previous_label="other", corrected_label="offer", used_in_prompt_version_id=version.id),
    ]
    db.add_all(rows)
    db.commit()
    first_id, used_id = rows[0].id, rows[2].id
    db.close()

    client = _client(SessionLocal)
    assert client.delete(f"/api/classify/training/{used_id}").status_code == 409
    assert client.delete("/api/classify/training/999999").status_code == 404
    single = client.delete(f"/api/classify/training/{first_id}")
    assert single.status_code == 200
    assert single.json()["deleted"] == 1
    bulk = client.delete("/api/classify/training/unused")
    assert bulk.status_code == 200
    assert bulk.json()["deleted"] == 1
    listed = client.get("/api/classify/training").json()
    assert listed["total"] == 1
    assert listed["unused_count"] == 0
    assert listed["items"][0]["id"] == used_id


def test_classify_email_uses_db_prompt_not_seed(monkeypatch):
    monkeypatch.setattr("app.classify.openai_classifier.has_saved_ai_settings", lambda: False)
    updated = SYSTEM_PROMPT + "\n- Unique marker from the SQLite active prompt.\n"
    captured: dict = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return SimpleNamespace(
                id="resp_cls",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps({"label": "other", "confidence": 0.9})
                        )
                    )
                ],
            )

    monkeypatch.setattr(
        "app.classify.openai_classifier.get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        "app.classify.openai_classifier.AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )
    monkeypatch.setattr("app.classify.prompt_store._load_from_db", lambda: updated.strip())
    invalidate_prompt_cache()

    asyncio.run(
        classify_email(
            subject="Hello",
            sender="recruiter@example.com",
            body_text="A generic note.",
            snippet="A generic note.",
        )
    )
    system = captured["messages"][0]["content"]
    assert "Unique marker from the SQLite active prompt" in system
    assert system != SYSTEM_PROMPT
    invalidate_prompt_cache()


def test_prompt_update_preserves_taxonomy_when_model_returns_rules_only(monkeypatch):
    monkeypatch.setattr("app.services.prompt_update.has_saved_ai_settings", lambda: False)
    SessionLocal = _session_factory()
    db = SessionLocal()
    email = _seed_email(db)
    db.add(
        ClassifyCorrection(
            email_id=email.id,
            previous_label=EmailLabel.OTHER.value,
            corrected_label=EmailLabel.SCREENING.value,
            subject=email.subject,
            sender=email.sender,
            snippet=email.snippet,
            body_text=email.body_text,
        )
    )
    db.commit()

    learned_rules = (
        "When a recruiter asks the candidate for availability before any confirmed "
        "meeting time exists, classify the message as screening. This rule should "
        "override generic scheduling words in quoted text."
    )

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                id="resp_rules_only",
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"prompt": learned_rules})))],
            )

    monkeypatch.setattr(
        "app.services.prompt_update.get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        "app.services.prompt_update.AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )

    result = asyncio.run(update_prompt_from_corrections(db))
    assert result.ok is True
    active = db.query(ClassifyPromptVersion).filter(ClassifyPromptVersion.is_active.is_(True)).one()
    assert learned_rules in active.prompt_text
    assert "Additional rules learned from training:" in active.prompt_text
    for classify_type in CLASSIFY_TYPES:
        assert classify_type.slug in active.prompt_text
    db.close()
    invalidate_prompt_cache()

def test_openai_subtype_overrides_keyword_inference(monkeypatch):
    monkeypatch.setattr("app.classify.openai_classifier.has_saved_ai_settings", lambda: False)
    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(id="resp_subtype", choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"label": "interview_scheduled", "confidence": 0.9, "interview_subtype": "confirmation"})
            ))])

    monkeypatch.setattr("app.classify.openai_classifier.get_settings", lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="gpt-4o-mini"))
    monkeypatch.setattr("app.classify.openai_classifier.AsyncOpenAI", lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))
    result = asyncio.run(classify_email(
        subject="Interview confirmed for Friday",
        sender="recruiter@example.com",
        body_text="If you need to reschedule, contact us. Your interview is confirmed for Friday at 2 PM.",
        snippet="Interview confirmed",
    ))
    assert result.label == "interview_scheduled"
    assert result.interview_subtype == "confirmation"

def test_classify_email_uses_activated_prompt_immediately(monkeypatch):
    monkeypatch.setattr("app.classify.openai_classifier.has_saved_ai_settings", lambda: False)
    updated = SYSTEM_PROMPT + "\n- Immediate cache marker after Update prompt.\n"
    captured: dict = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return SimpleNamespace(
                id="resp_cls2",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps({"label": "other", "confidence": 0.8})
                        )
                    )
                ],
            )

    monkeypatch.setattr(
        "app.classify.openai_classifier.get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        "app.classify.openai_classifier.AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )
    monkeypatch.setattr("app.classify.prompt_store._load_from_db", lambda: SYSTEM_PROMPT)
    invalidate_prompt_cache()
    activate_prompt_text(updated)

    asyncio.run(
        classify_email(
            subject="Hello",
            sender="recruiter@example.com",
            body_text="A generic note.",
            snippet="A generic note.",
        )
    )
    system = captured["messages"][0]["content"]
    assert "Immediate cache marker after Update prompt" in system
    invalidate_prompt_cache()

