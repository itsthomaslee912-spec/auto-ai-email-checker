import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import emails, mailboxes
from app.classify.outcome_extract import BACKFILL_LABELS, OUTCOME_LABELS, apply_outcome, parse_company_role
from app.db import Base, get_db
from app.models import EmailLabel, EmailMessage, MailboxConnection, Provider, User


def test_parse_company_role_reads_json_and_rejects_junk():
    assert parse_company_role('{"company":"Cloudbeds","role":"Senior Software Engineer"}') == (
        "Cloudbeds",
        "Senior Software Engineer",
    )
    assert parse_company_role('{"company":"Acme"}') == ("Acme", "")
    assert parse_company_role('{"role":"  Staff   Engineer  "}') == ("", "Staff Engineer")
    assert parse_company_role("not json") == ("", "")
    assert parse_company_role('{"company":1,"role":null}') == ("", "")
    assert parse_company_role("[1, 2]") == ("", "")


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _client(SessionLocal, *routers):
    app = FastAPI()
    for router in routers:
        app.include_router(router)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _seed_mailbox(db, *, email_address: str) -> MailboxConnection:
    user = db.query(User).filter(User.external_id == "outcomes-user").one_or_none()
    if user is None:
        user = User(external_id="outcomes-user")
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


def _add_email(
    db,
    mailbox_id: int,
    *,
    label: str,
    received_at: datetime,
    provider_message_id: str,
    company: str | None = None,
    job_role: str | None = None,
    subject: str = "",
    outcome_extracted: bool = False,
) -> EmailMessage:
    email = EmailMessage(
        mailbox_id=mailbox_id,
        provider_message_id=provider_message_id,
        subject=subject or f"{label} mail",
        sender="recruiter@example.com",
        snippet="snippet",
        body_text="body",
        label=label,
        received_at=received_at,
        company=company,
        job_role=job_role,
        outcome_extracted=outcome_extracted,
    )
    db.add(email)
    return email


def test_outcomes_reads_stored_rows_without_calling_openai():
    SessionLocal = _session_factory()
    db = SessionLocal()
    box_a = _seed_mailbox(db, email_address="a@example.com")
    box_b = _seed_mailbox(db, email_address="b@example.com")
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 8, 10, 12, 0, 0),
        provider_message_id="rej-old",
        company="Acme",
        job_role="Engineer",
        outcome_extracted=True,
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 8, 20, 9, 0, 0),
        provider_message_id="rej-new",
        company="acme",
        job_role="engineer",
        outcome_extracted=True,
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 8, 11, 9, 0, 0),
        provider_message_id="rej-blank-1",
        company="",
        job_role="",
        subject="No company one",
        outcome_extracted=True,
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 8, 12, 9, 0, 0),
        provider_message_id="rej-blank-2",
        company="",
        job_role="",
        subject="No company two",
        outcome_extracted=True,
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.SCREENING.value,
        received_at=datetime(2026, 8, 15, 9, 0, 0),
        provider_message_id="screen",
        company="Stripe",
        job_role="Recruiter screen",
        outcome_extracted=True,
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
        received_at=datetime(2026, 8, 16, 9, 0, 0),
        provider_message_id="interview_scheduled",
        company="Notion",
        job_role="Product Engineer",
        outcome_extracted=True,
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.APPLICATION_CONFIRMATION.value,
        received_at=datetime(2026, 8, 17, 9, 0, 0),
        provider_message_id="application_confirmation",
        company="Ignored Co",
        job_role="Ignored Role",
    )
    _add_email(
        db,
        box_a.id,
        label=EmailLabel.REJECTED_CLOSED.value,
        received_at=datetime(2026, 1, 2, 9, 0, 0),
        provider_message_id="out-of-range",
        company="Old Co",
        job_role="Old Role",
        outcome_extracted=True,
    )
    _add_email(
        db,
        box_b.id,
        label=EmailLabel.INTERVIEW_SCHEDULED.value,
        received_at=datetime(2026, 8, 18, 9, 0, 0),
        provider_message_id="other-box",
        company="Other",
        job_role="Other Role",
        outcome_extracted=True,
    )
    db.commit()
    mailbox_id = box_a.id
    db.close()

    client = _client(SessionLocal, mailboxes.router)
    extract = AsyncMock(return_value=("Should Not", "Run"))
    with patch("app.classify.outcome_extract.extract_company_role", extract):
        res = client.get(
            f"/api/mailboxes/{mailbox_id}/outcomes",
            params={"date_from": "2026-08-01", "date_to": "2026-08-31"},
        )
    assert res.status_code == 200, res.text
    assert extract.await_count == 0
    body = res.json()
    assert body["mailbox_id"] == mailbox_id
    assert len(body["rejected_closed"]) == 3
    assert body["rejected_closed"][0]["company"] == "acme"
    assert body["rejected_closed"][0]["role"] == "engineer"
    assert body["rejected_closed"][0]["received_at"].startswith("2026-08-20")
    subjects = {item["subject"] for item in body["rejected_closed"] if not item["company"]}
    assert subjects == {"No company one", "No company two"}
    assert body["screening"] == [
        {
            "company": "Stripe",
            "role": "Recruiter screen",
            "received_at": body["screening"][0]["received_at"],
            "subject": "screening mail",
        }
    ]
    assert body["screening"][0]["received_at"].startswith("2026-08-15")
    assert body["interview_scheduled"][0]["company"] == "Notion"
    assert body["interview_scheduled"][0]["role"] == "Product Engineer"
    assert body["application_confirmation"] == [
        {
            "company": "Ignored Co",
            "role": "Ignored Role",
            "received_at": body["application_confirmation"][0]["received_at"],
            "subject": "application_confirmation mail",
        }
    ]
    assert body["application_confirmation"][0]["received_at"].startswith("2026-08-17")
    assert all(item["company"] != "Ignored Co" for item in body["rejected_closed"])
    assert all(item["company"] != "Ignored Co" for item in body["screening"])
    assert all(item["company"] != "Ignored Co" for item in body["interview_scheduled"])
    assert all(item["company"] != "Old Co" for item in body["rejected_closed"])
    assert all(item["company"] != "Other" for item in body["interview_scheduled"])

    page = client.get(
        f"/api/mailboxes/{mailbox_id}/outcomes",
        params={
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "label": "rejected_closed",
            "limit": 1,
            "offset": 0,
        },
    )
    assert page.status_code == 200, page.text
    page_body = page.json()
    assert page_body["total"] == 3
    assert page_body["label"] == "rejected_closed"
    assert len(page_body["items"]) == 1
    assert page_body["items"][0]["company"] == "acme"
    assert page_body["rejected_closed"] == []


def test_label_update_extracts_only_for_outcome_labels():
    SessionLocal = _session_factory()
    db = SessionLocal()
    box = _seed_mailbox(db, email_address="me@example.com")
    rejected_target = _add_email(
        db,
        box.id,
        label=EmailLabel.APPLICATION_CONFIRMATION.value,
        received_at=datetime(2026, 8, 1, 12, 0, 0),
        provider_message_id="to-rejected",
        subject="Thanks for applying to Acme",
    )
    other_target = _add_email(
        db,
        box.id,
        label=EmailLabel.OTHER.value,
        received_at=datetime(2026, 8, 2, 12, 0, 0),
        provider_message_id="to-alert",
        subject="Newsletter",
    )
    applied_target = _add_email(
        db,
        box.id,
        label=EmailLabel.RECRUITMENT_ALERT.value,
        received_at=datetime(2026, 8, 3, 12, 0, 0),
        provider_message_id="to-applied",
        subject="Thanks for applying to Stripe",
    )
    db.commit()
    rejected_id = rejected_target.id
    other_id = other_target.id
    applied_id = applied_target.id
    db.close()

    client = _client(SessionLocal, emails.router)
    extract = AsyncMock(return_value=("Acme", "Backend Engineer"))
    with patch("app.classify.outcome_extract.extract_company_role", extract):
        rejected = client.patch(
            f"/api/emails/{rejected_id}/label",
            json={"label": "rejected_closed", "save_training": False},
        )
        other = client.patch(
            f"/api/emails/{other_id}/label",
            json={"label": "recruitment_alert", "save_training": False},
        )
        applied = client.patch(
            f"/api/emails/{applied_id}/label",
            json={"label": "application_confirmation", "save_training": False},
        )

    assert rejected.status_code == 200, rejected.text
    assert other.status_code == 200, other.text
    assert applied.status_code == 200, applied.text
    assert extract.await_count == 2

    db = SessionLocal()
    stored = db.query(EmailMessage).filter(EmailMessage.id == rejected_id).one()
    assert stored.company == "Acme"
    assert stored.job_role == "Backend Engineer"
    assert stored.outcome_extracted is True
    applied_row = db.query(EmailMessage).filter(EmailMessage.id == applied_id).one()
    assert applied_row.label == EmailLabel.APPLICATION_CONFIRMATION.value
    assert applied_row.company == "Acme"
    assert applied_row.job_role == "Backend Engineer"
    assert applied_row.outcome_extracted is True
    untouched = db.query(EmailMessage).filter(EmailMessage.id == other_id).one()
    assert untouched.label == EmailLabel.RECRUITMENT_ALERT.value
    assert untouched.outcome_extracted is False
    assert untouched.company is None
    db.close()


def test_new_applied_mail_extracts_company_and_role():
    assert EmailLabel.APPLICATION_CONFIRMATION.value in OUTCOME_LABELS
    assert EmailLabel.APPLICATION_CONFIRMATION.value not in BACKFILL_LABELS

    SessionLocal = _session_factory()
    db = SessionLocal()
    box = _seed_mailbox(db, email_address="new@example.com")
    email = _add_email(
        db,
        box.id,
        label=EmailLabel.APPLICATION_CONFIRMATION.value,
        received_at=datetime(2026, 9, 1, 12, 0, 0),
        provider_message_id="new-applied",
        subject="Thanks for applying to Notion",
    )
    db.commit()
    email_id = email.id

    async def run() -> int:
        extract = AsyncMock(return_value=("Notion", "Product Engineer"))
        with patch("app.classify.outcome_extract.extract_company_role", extract):
            saved = await apply_outcome(db, email)
        assert saved is True
        db.commit()
        return extract.await_count

    calls = asyncio.run(run())
    assert calls == 1
    db.close()

    db = SessionLocal()
    stored = db.query(EmailMessage).filter(EmailMessage.id == email_id).one()
    assert stored.company == "Notion"
    assert stored.job_role == "Product Engineer"
    assert stored.outcome_extracted is True
    db.close()
