from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()
database_url = make_url(settings.database_url)
if database_url.get_backend_name() == "sqlite" and database_url.database not in (None, ":memory:"):
    database_path = Path(database_url.database)
    if not database_path.is_absolute():
        database_path = (Path(__file__).resolve().parents[1] / database_path).resolve()
        database_url = database_url.set(database=str(database_path))

connect_args = {}
if database_url.get_backend_name() == "sqlite":
    connect_args = {"check_same_thread": False, "timeout": 60}

engine = create_engine(database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


LABEL_SLUG_REMAPS = (
    ("tech", "assessment"),
    ("available", "interview_invitation"),
    ("alert", "recruitment_alert"),
    ("application_submitted", "application_confirmation"),
    ("new_opportunity", "recruitment_alert"),
    ("recruiter_outreach", "recruitment_alert"),
    ("talent_pool", "other"),
    ("company_news", "other"),
    ("career_event", "other"),
    ("profile_update_request", "application_action_required"),
    ("withdrawn", "rejected_closed"),
    ("hired", "offer"),
    ("job_alert", "recruitment_alert"),
    ("applied", "application_confirmation"),
    ("interview", "interview_invitation"),
    ("rejected", "rejected_closed"),
    ("others", "other"),
)


def seed_default_classify_prompt() -> None:
    from app.classify.prompt import SYSTEM_PROMPT
    from app.models import ClassifyPromptVersion

    db = SessionLocal()
    try:
        exists = db.query(ClassifyPromptVersion.id).first()
        if exists is None:
            db.add(
                ClassifyPromptVersion(
                    prompt_text=SYSTEM_PROMPT,
                    is_active=True,
                    source="seed",
                    example_count=0,
                )
            )
            db.commit()
    finally:
        db.close()


def _is_current_taxonomy_prompt(text: str) -> bool:
    if not text:
        return False
    from app.models import EmailLabel
    return all(f"- {item.value}" in text for item in EmailLabel)



def ensure_current_taxonomy_prompt() -> bool:
    """Activate the eleven-label prompt when the stored prompt uses an older taxonomy.

    Does not start a reclassify job. Returns True only when a new version was inserted.
    """
    from app.classify.prompt import SYSTEM_PROMPT
    from app.classify.prompt_store import activate_prompt_text
    from app.models import ClassifyPromptVersion

    db = SessionLocal()
    try:
        active = (
            db.query(ClassifyPromptVersion)
            .filter(ClassifyPromptVersion.is_active.is_(True))
            .order_by(ClassifyPromptVersion.id.desc())
            .first()
        )
        text = (active.prompt_text if active else "") or ""
        if _is_current_taxonomy_prompt(text):
            return False
        if active is not None:
            active.is_active = False
        db.add(
            ClassifyPromptVersion(
                prompt_text=SYSTEM_PROMPT,
                is_active=True,
                source="taxonomy_v4",
                example_count=0,
            )
        )
        db.commit()
        activate_prompt_text(SYSTEM_PROMPT)
        return True
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401
    from sqlalchemy import inspect, text

    Base.metadata.create_all(bind=engine)

    # Lightweight SQLite column / label migrations for existing DBs
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as conn:
            tables = inspect(conn).get_table_names()
            if "email_messages" in tables:
                cols = {c["name"] for c in inspect(conn).get_columns("email_messages")}
                if "body_html" not in cols:
                    conn.execute(text("ALTER TABLE email_messages ADD COLUMN body_html TEXT DEFAULT ''"))
                if "is_read" not in cols:
                    conn.execute(text("ALTER TABLE email_messages ADD COLUMN is_read BOOLEAN DEFAULT 0"))
                if "human_corrected" not in cols:
                    conn.execute(
                        text(
                            "ALTER TABLE email_messages ADD COLUMN human_corrected BOOLEAN DEFAULT 0"
                        )
                    )
                if "classification_pending" not in cols:
                    conn.execute(
                        text(
                            "ALTER TABLE email_messages "
                            "ADD COLUMN classification_pending BOOLEAN DEFAULT 0"
                        )
                    )
                if "folder" not in cols:
                    conn.execute(
                        text(
                            "ALTER TABLE email_messages ADD COLUMN folder VARCHAR(16) DEFAULT 'inbox'"
                        )
                    )
                    conn.execute(
                        text("UPDATE email_messages SET folder = 'inbox' WHERE folder IS NULL")
                    )
                if "company" not in cols:
                    conn.execute(text("ALTER TABLE email_messages ADD COLUMN company VARCHAR(255)"))
                if "job_role" not in cols:
                    conn.execute(text("ALTER TABLE email_messages ADD COLUMN job_role VARCHAR(255)"))
                if "outcome_extracted" not in cols:
                    conn.execute(
                        text(
                            "ALTER TABLE email_messages ADD COLUMN outcome_extracted BOOLEAN DEFAULT 0"
                        )
                    )
                if "interview_subtype" not in cols:
                    conn.execute(text("ALTER TABLE email_messages ADD COLUMN interview_subtype VARCHAR(32)"))
                from app.classify.openai_classifier import (
                    _combined_text,
                    _is_scheduled_interview,
                    infer_interview_subtype,
                )

                old_interviews = conn.execute(text(
                    "SELECT id, subject, sender, body_text, snippet FROM email_messages "
                    "WHERE label = 'interview'"
                )).mappings()
                for row in old_interviews:
                    subject = row["subject"] or ""
                    sender = row["sender"] or ""
                    body = row["body_text"] or ""
                    snippet = row["snippet"] or ""
                    if _is_scheduled_interview(_combined_text(subject, sender, body, snippet)):
                        conn.execute(text(
                            "UPDATE email_messages SET label = 'interview_scheduled', "
                            "interview_subtype = :subtype WHERE id = :id"
                        ), {"id": row["id"], "subtype": infer_interview_subtype(subject, sender, body, snippet)})
                for old, new in LABEL_SLUG_REMAPS:
                    conn.execute(
                        text("UPDATE email_messages SET label = :new WHERE label = :old"),
                        {"new": new, "old": old},
                    )
                conn.execute(
                    text(
                        "UPDATE email_messages SET label = 'other', classification_pending = 1 "
                        "WHERE label = 'unknown' AND COALESCE(human_corrected, 0) = 0"
                    )
                )
                conn.execute(text("UPDATE email_messages SET label = 'other' WHERE label = 'unknown'"))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_email_messages_received_id "
                        "ON email_messages (received_at, id)"
                    )
                )
            if "ai_settings" in tables:
                ai_cols = {c["name"] for c in inspect(conn).get_columns("ai_settings")}
                if "openai_admin_key_enc" not in ai_cols:
                    conn.execute(text("ALTER TABLE ai_settings ADD COLUMN openai_admin_key_enc TEXT DEFAULT ''"))
            if "mailbox_connections" in tables:
                mb_cols = {c["name"] for c in inspect(conn).get_columns("mailbox_connections")}
                if "full_sync_completed" not in mb_cols:
                    conn.execute(
                        text(
                            "ALTER TABLE mailbox_connections "
                            "ADD COLUMN full_sync_completed BOOLEAN DEFAULT 0"
                        )
                    )
                if "received_at_utc_fixed" not in mb_cols:
                    conn.execute(
                        text(
                            "ALTER TABLE mailbox_connections "
                            "ADD COLUMN received_at_utc_fixed BOOLEAN DEFAULT 0"
                        )
                    )
            if "classify_corrections" in tables:
                correction_cols = {c["name"] for c in inspect(conn).get_columns("classify_corrections")}
                if "previous_subtype" not in correction_cols:
                    conn.execute(text("ALTER TABLE classify_corrections ADD COLUMN previous_subtype VARCHAR(32)"))
                if "corrected_subtype" not in correction_cols:
                    conn.execute(text("ALTER TABLE classify_corrections ADD COLUMN corrected_subtype VARCHAR(32)"))
                conn.execute(text(
                    "UPDATE classify_corrections SET corrected_label = 'interview_scheduled' "
                    "WHERE corrected_label = 'interview' AND email_id IN "
                    "(SELECT id FROM email_messages WHERE label = 'interview_scheduled')"
                ))
                for old, new in LABEL_SLUG_REMAPS:
                    conn.execute(
                        text(
                            "UPDATE classify_corrections SET previous_label = :new "
                            "WHERE previous_label = :old"
                        ),
                        {"new": new, "old": old},
                    )
                    conn.execute(
                        text(
                            "UPDATE classify_corrections SET corrected_label = :new "
                            "WHERE corrected_label = :old"
                        ),
                        {"new": new, "old": old},
                    )
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA busy_timeout=60000"))
            conn.commit()

    seed_default_classify_prompt()
    from app.services.mailbox_cleanup import purge_orphaned_mailbox_mail

    purge_orphaned_mailbox_mail()
    ensure_current_taxonomy_prompt()
