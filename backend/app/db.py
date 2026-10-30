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
engine_kwargs: dict = {"pool_pre_ping": True, "pool_timeout": 10}
if database_url.get_backend_name() == "sqlite":
    connect_args = {"check_same_thread": False, "timeout": 60}
else:
    if database_url.get_backend_name() == "postgresql":
        lock_timeout_ms = max(100, int(settings.database_lock_timeout_seconds * 1000))
        idle_transaction_timeout_ms = max(
            1000, int(settings.database_idle_transaction_timeout_seconds * 1000)
        )
        connect_args = {
            "options": (
                f"-c lock_timeout={lock_timeout_ms} "
                f"-c idle_in_transaction_session_timeout={idle_transaction_timeout_ms}"
            )
        }
    # Drop connections older than this so a restarted PostgreSQL is picked up
    # instead of leaving requests stuck on a dead socket.
    engine_kwargs["pool_recycle"] = max(30, settings.database_pool_recycle_seconds)
    engine_kwargs["pool_size"] = max(1, settings.database_pool_size)
    engine_kwargs["max_overflow"] = max(0, settings.database_max_overflow)
    engine_kwargs["pool_timeout"] = max(0.1, settings.database_pool_timeout_seconds)

engine = create_engine(database_url, connect_args=connect_args, **engine_kwargs)
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


DEFAULT_ADMIN_EMAIL = "admin123@gmail.com"
DEFAULT_ADMIN_PASSWORD = "123"


def seed_admin_account() -> None:
    """Make sure at least one admin can sign in.

    The pre-auth installation kept every mailbox under a placeholder user
    ("default"); that user becomes the admin so existing mail stays visible.
    """
    from app.auth.session import hash_password
    from app.models import User

    db = SessionLocal()
    try:
        if db.query(User.id).filter(User.email.is_not(None), User.is_admin.is_(True)).first():
            return
        user = (
            db.query(User).filter(User.external_id == "default").one_or_none()
            or db.query(User).filter(User.email.is_(None)).order_by(User.id).first()
        )
        if user is None:
            user = User(external_id=f"user:{DEFAULT_ADMIN_EMAIL}")
            db.add(user)
        user.email = DEFAULT_ADMIN_EMAIL
        user.password_hash = hash_password(DEFAULT_ADMIN_PASSWORD)
        user.display_name = user.display_name or "Admin"
        user.is_admin = True
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


def _ensure_search_index(conn) -> None:
    """Trigram index so inbox search does not scan every message body."""
    import logging

    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from app.models import email_search_document_sql

    try:
        with conn.begin_nested():
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_email_messages_search_trgm ON email_messages "
                f"USING gin (({email_search_document_sql('')}) gin_trgm_ops)"
            ))
    except SQLAlchemyError as exc:
        logging.getLogger(__name__).warning("Search index unavailable; search will scan: %s", exc)


def init_db() -> None:
    from app import models  # noqa: F401
    from sqlalchemy import inspect, text

    # The API and every native worker start at roughly the same time and each
    # calls init_db(). Serialize PostgreSQL schema initialization so two
    # processes cannot both inspect a missing column and then race to add it.
    # The transaction-scoped advisory lock is released automatically on commit,
    # rollback, or process failure.
    with engine.begin() as conn:
        if database_url.get_backend_name() == "postgresql":
            conn.execute(text(
                "SELECT pg_advisory_xact_lock(hashtext('auto_ai_email_checker_schema_init'))"
            ))

        Base.metadata.create_all(bind=conn)

        # Webhook delivery health must survive application restarts. These
        # columns are safe to add on both PostgreSQL and SQLite installations.
        tables = inspect(conn).get_table_names()
        if "webhook_subscriptions" in tables:
            webhook_cols = {
                c["name"] for c in inspect(conn).get_columns("webhook_subscriptions")
            }
            timestamp_type = (
                "TIMESTAMP WITH TIME ZONE"
                if database_url.get_backend_name() == "postgresql"
                else "TIMESTAMP"
            )
            if "last_delivery_success_at" not in webhook_cols:
                conn.execute(text(
                    "ALTER TABLE webhook_subscriptions "
                    f"ADD COLUMN last_delivery_success_at {timestamp_type}"
                ))
            if "last_delivery_failure_at" not in webhook_cols:
                conn.execute(text(
                    "ALTER TABLE webhook_subscriptions "
                    f"ADD COLUMN last_delivery_failure_at {timestamp_type}"
                ))
            if "last_delivery_error" not in webhook_cols:
                conn.execute(text(
                    "ALTER TABLE webhook_subscriptions "
                    "ADD COLUMN last_delivery_error VARCHAR(512)"
                ))
        if "ai_settings" in tables:
            ai_cols = {c["name"] for c in inspect(conn).get_columns("ai_settings")}
            if "gemini_key_enc" not in ai_cols:
                conn.execute(text("ALTER TABLE ai_settings ADD COLUMN gemini_key_enc TEXT DEFAULT ''"))
            if "gemini_model" not in ai_cols:
                conn.execute(text(
                    "ALTER TABLE ai_settings ADD COLUMN gemini_model "
                    "VARCHAR(128) DEFAULT 'gemini-3.8-flash'"
                ))
        if "email_messages" in tables:
            email_cols = {c["name"] for c in inspect(conn).get_columns("email_messages")}
            timestamp_type = (
                "TIMESTAMP WITH TIME ZONE"
                if database_url.get_backend_name() == "postgresql"
                else "TIMESTAMP"
            )
            if "calendar_processed_hash" not in email_cols:
                conn.execute(text(
                    "ALTER TABLE email_messages ADD COLUMN calendar_processed_hash VARCHAR(64)"
                ))
            if "calendar_processed_at" not in email_cols:
                conn.execute(text(
                    "ALTER TABLE email_messages "
                    f"ADD COLUMN calendar_processed_at {timestamp_type}"
                ))
            if "calendar_processing_error" not in email_cols:
                conn.execute(text(
                    "ALTER TABLE email_messages ADD COLUMN calendar_processing_error VARCHAR(512)"
                ))
            if "pending_dismissed_at" not in email_cols:
                conn.execute(text(
                    f"ALTER TABLE email_messages ADD COLUMN pending_dismissed_at {timestamp_type}"
                ))
            if "category_subtype" not in email_cols:
                conn.execute(text(
                    "ALTER TABLE email_messages ADD COLUMN category_subtype VARCHAR(48)"
                ))
            if "recipients" not in email_cols:
                conn.execute(text(
                    "ALTER TABLE email_messages ADD COLUMN recipients TEXT DEFAULT ''"
                ))
        if "calendar_events" in tables:
            calendar_cols = {c["name"] for c in inspect(conn).get_columns("calendar_events")}
            if "interviewer" not in calendar_cols:
                conn.execute(text(
                    "ALTER TABLE calendar_events ADD COLUMN interviewer VARCHAR(255) DEFAULT ''"
                ))
            if "work_model" not in calendar_cols:
                conn.execute(text(
                    "ALTER TABLE calendar_events ADD COLUMN work_model VARCHAR(32) DEFAULT 'unspecified'"
                ))
            if "company_human_corrected" not in calendar_cols:
                conn.execute(text(
                    "ALTER TABLE calendar_events "
                    "ADD COLUMN company_human_corrected BOOLEAN DEFAULT FALSE"
                ))
            if {"interview_status", "is_visible"}.issubset(calendar_cols):
                # Cancellations used to be hidden. Preserve those historical
                # appointments and let the calendar render their status.
                # A superseded row is a discarded duplicate, not that appointment.
                hidden_cancellations = (
                    "UPDATE calendar_events SET is_visible = :visible "
                    "WHERE interview_status = 'cancellation' AND is_visible = :hidden"
                )
                if "superseded_by_event_id" in calendar_cols:
                    hidden_cancellations += " AND superseded_by_event_id IS NULL"
                conn.execute(
                    text(hidden_cancellations),
                    {"visible": True, "hidden": False},
                )
        if "mailbox_connections" in tables:
            mailbox_cols = {c["name"] for c in inspect(conn).get_columns("mailbox_connections")}
            if "calendar_color" not in mailbox_cols:
                conn.execute(text(
                    "ALTER TABLE mailbox_connections ADD COLUMN calendar_color VARCHAR(7)"
                ))
        if "mailbox_job_states" in tables:
            job_cols = {c["name"] for c in inspect(conn).get_columns("mailbox_job_states")}
            timestamp_type = (
                "TIMESTAMP WITH TIME ZONE"
                if database_url.get_backend_name() == "postgresql"
                else "TIMESTAMP"
            )
            for column, ddl in (
                ("claimed_by", "VARCHAR(160)"),
                ("lease_expires_at", timestamp_type),
                ("attempts", "INTEGER DEFAULT 0"),
                ("next_retry_at", timestamp_type),
                ("last_error", "VARCHAR(512)"),
            ):
                if column not in job_cols:
                    conn.execute(text(
                        f"ALTER TABLE mailbox_job_states ADD COLUMN {column} {ddl}"
                    ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_mailbox_jobs_lease "
                "ON mailbox_job_states (job_type, state, lease_expires_at)"
            ))
        if "service_heartbeats" in tables:
            heartbeat_cols = {c["name"] for c in inspect(conn).get_columns("service_heartbeats")}
            if "status" not in heartbeat_cols:
                conn.execute(text(
                    "ALTER TABLE service_heartbeats ADD COLUMN status VARCHAR(16) DEFAULT 'healthy'"
                ))
                conn.execute(text(
                    "UPDATE service_heartbeats SET status = 'healthy' WHERE status IS NULL"
                ))
        if "classify_corrections" in tables:
            correction_cols = {c["name"] for c in inspect(conn).get_columns("classify_corrections")}
            if "evaluation_holdout" not in correction_cols:
                false_default = "FALSE" if database_url.get_backend_name() == "postgresql" else "0"
                conn.execute(text(
                    "ALTER TABLE classify_corrections ADD COLUMN evaluation_holdout "
                    f"BOOLEAN DEFAULT {false_default}"
                ))
                # Reserve a stable 20% of historical corrections. These rows are
                # never used in prompts or retrieval.
                conn.execute(text(
                    "UPDATE classify_corrections SET evaluation_holdout = :held_out "
                    "WHERE MOD(id, 5) = 0 AND used_in_prompt_version_id IS NULL"
                ), {"held_out": True})
        if "users" in tables:
            user_cols = {c["name"] for c in inspect(conn).get_columns("users")}
            false_default = "FALSE" if database_url.get_backend_name() == "postgresql" else "0"
            timestamp_type = (
                "TIMESTAMP WITH TIME ZONE"
                if database_url.get_backend_name() == "postgresql"
                else "TIMESTAMP"
            )
            for column, ddl in (
                ("email", "VARCHAR(320)"),
                ("password_hash", "VARCHAR(255)"),
                ("display_name", "VARCHAR(255)"),
                ("is_admin", f"BOOLEAN DEFAULT {false_default}"),
                ("last_login_at", timestamp_type),
            ):
                if column not in user_cols:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {column} {ddl}"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)"))

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
                conn.execute(text("UPDATE email_messages SET is_read = 0 WHERE is_read IS NULL"))
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
                if "category_subtype" not in cols:
                    conn.execute(text("ALTER TABLE email_messages ADD COLUMN category_subtype VARCHAR(48)"))
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
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_email_messages_mailbox_received "
                    "ON email_messages (mailbox_id, received_at, id)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_email_messages_mailbox_label_received "
                    "ON email_messages (mailbox_id, label, received_at, id)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_email_messages_mailbox_folder_received "
                    "ON email_messages (mailbox_id, folder, received_at, id)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_email_messages_mailbox_unread_received "
                    "ON email_messages (mailbox_id, is_read, received_at, id)"
                ))
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
                if "last_sync_started_at" not in mb_cols:
                    conn.execute(text(
                        "ALTER TABLE mailbox_connections "
                        "ADD COLUMN last_sync_started_at TIMESTAMP"
                    ))
                if "last_sync_completed_at" not in mb_cols:
                    conn.execute(text(
                        "ALTER TABLE mailbox_connections "
                        "ADD COLUMN last_sync_completed_at TIMESTAMP"
                    ))
                if "last_sync_error" not in mb_cols:
                    conn.execute(text(
                        "ALTER TABLE mailbox_connections "
                        "ADD COLUMN last_sync_error VARCHAR(512)"
                    ))
                if "calendar_color" not in mb_cols:
                    conn.execute(text(
                        "ALTER TABLE mailbox_connections "
                        "ADD COLUMN calendar_color VARCHAR(7)"
                    ))
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

    if database_url.get_backend_name() == "postgresql":
        with engine.begin() as conn:
            # Keep the PostgreSQL-only backfills/index creation under the same
            # cross-process startup lock as the portable migrations above.
            conn.execute(text(
                "SELECT pg_advisory_xact_lock(hashtext('auto_ai_email_checker_schema_init'))"
            ))
            if "email_messages" in inspect(conn).get_table_names():
                # Backfill rows where is_read is NULL (added before server DEFAULT was set).
                conn.execute(text("UPDATE email_messages SET is_read = FALSE WHERE is_read IS NULL"))
                index_statements = {
                    "ix_email_messages_received_id":
                        "CREATE INDEX ix_email_messages_received_id "
                        "ON email_messages (received_at DESC NULLS LAST, id DESC)",
                    "ix_email_messages_label_received":
                        "CREATE INDEX ix_email_messages_label_received "
                        "ON email_messages (label, received_at DESC NULLS LAST, id DESC)",
                    "ix_email_messages_folder_received":
                        "CREATE INDEX ix_email_messages_folder_received "
                        "ON email_messages (folder, received_at DESC NULLS LAST, id DESC)",
                    "ix_email_messages_sidebar_counts":
                        "CREATE INDEX ix_email_messages_sidebar_counts "
                        "ON email_messages (mailbox_id, label, folder, is_read)",
                    "ix_email_messages_thread":
                        "CREATE INDEX ix_email_messages_thread "
                        "ON email_messages (mailbox_id, thread_id, received_at DESC, id DESC)",
                    # Mailbox-scoped list queries filter by mailbox_id then sort.
                    "ix_email_messages_mailbox_received":
                        "CREATE INDEX ix_email_messages_mailbox_received "
                        "ON email_messages (mailbox_id, received_at DESC NULLS LAST, id DESC)",
                    "ix_email_messages_mailbox_label_received":
                        "CREATE INDEX ix_email_messages_mailbox_label_received "
                        "ON email_messages (mailbox_id, label, received_at DESC NULLS LAST, id DESC)",
                    "ix_email_messages_mailbox_folder_received":
                        "CREATE INDEX ix_email_messages_mailbox_folder_received "
                        "ON email_messages (mailbox_id, folder, received_at DESC NULLS LAST, id DESC)",
                    "ix_email_messages_mailbox_unread_received":
                        "CREATE INDEX ix_email_messages_mailbox_unread_received "
                        "ON email_messages (mailbox_id, is_read, received_at DESC NULLS LAST, id DESC)",
                }
                for index_name, ddl in index_statements.items():
                    # CREATE INDEX IF NOT EXISTS still acquires a table lock
                    # before discovering that the index exists. Avoid that
                    # lock entirely during normal application restarts.
                    exists = conn.execute(
                        text("SELECT to_regclass(:name) IS NOT NULL"),
                        {"name": index_name},
                    ).scalar()
                    if not exists:
                        conn.execute(text(ddl))
                search_index_exists = conn.execute(
                    text("SELECT to_regclass('ix_email_messages_search_trgm') IS NOT NULL")
                ).scalar()
                if not search_index_exists:
                    _ensure_search_index(conn)

    seed_default_classify_prompt()
    seed_admin_account()
    from app.services.mailbox_cleanup import purge_orphaned_mailbox_mail

    purge_orphaned_mailbox_mail()
    ensure_current_taxonomy_prompt()
