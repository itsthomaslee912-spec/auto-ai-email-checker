from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.timeutil import UtcDateTime


class Provider(str, Enum):
    GOOGLE = "google"
    MICROSOFT = "microsoft"


class EmailLabel(str, Enum):
    APPLICATION_CONFIRMATION = "application_confirmation"
    APPLICATION_ACTION_REQUIRED = "application_action_required"
    SCREENING = "screening"
    ASSESSMENT = "assessment"
    INTERVIEW_INVITATION = "interview_invitation"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    INTERVIEW_FOLLOW_UP = "interview_follow_up"
    OFFER = "offer"
    REJECTED_CLOSED = "rejected_closed"
    RECRUITMENT_ALERT = "recruitment_alert"
    OTHER = "other"


class MailFolder(str, Enum):
    INBOX = "inbox"
    SENT = "sent"
    SPAM = "spam"
    TRASH = "trash"
    ARCHIVE = "archive"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    mailboxes: Mapped[list[MailboxConnection]] = relationship(back_populates="user")


class MailboxConnection(Base):
    __tablename__ = "mailbox_connections"
    __table_args__ = (UniqueConstraint("provider", "email_address", name="uq_provider_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    email_address: Mapped[str] = mapped_column(String(320), index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Gmail history / Outlook delta cursors
    sync_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_sync_completed: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    # One-shot: received_at rewritten from provider UTC (fixes SQLite tz stripping)
    received_at_utc_fixed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="mailboxes")
    emails: Mapped[list[EmailMessage]] = relationship(
        back_populates="mailbox",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    webhook: Mapped[WebhookSubscription | None] = relationship(
        back_populates="mailbox",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (UniqueConstraint("mailbox_id", "provider_message_id", name="uq_mailbox_msg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailbox_connections.id", ondelete="CASCADE"), index=True
    )
    provider_message_id: Mapped[str] = mapped_column(String(255), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str] = mapped_column(String(998), default="")
    sender: Mapped[str] = mapped_column(String(512), default="")
    received_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    snippet: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    label: Mapped[str] = mapped_column(String(32), index=True, default=EmailLabel.OTHER.value)
    interview_subtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome_extracted: Mapped[bool] = mapped_column(default=False)
    folder: Mapped[str] = mapped_column(String(16), index=True, default=MailFolder.INBOX.value)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    openai_response_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_read: Mapped[bool] = mapped_column(default=False)
    human_corrected: Mapped[bool] = mapped_column(default=False)
    classification_pending: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    mailbox: Mapped[MailboxConnection] = relationship(back_populates="emails")
    corrections: Mapped[list[ClassifyCorrection]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ClassifyCorrection(Base):
    __tablename__ = "classify_corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("email_messages.id", ondelete="CASCADE"), index=True
    )
    previous_label: Mapped[str] = mapped_column(String(32))
    corrected_label: Mapped[str] = mapped_column(String(32), index=True)
    previous_subtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    corrected_subtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subject: Mapped[str] = mapped_column(String(998), default="")
    sender: Mapped[str] = mapped_column(String(512), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    used_in_prompt_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("classify_prompt_versions.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    email: Mapped[EmailMessage] = relationship(back_populates="corrections")


class ClassifyPromptVersion(Base):
    __tablename__ = "classify_prompt_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt_text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, index=True)
    source: Mapped[str] = mapped_column(String(32), default="seed")
    openai_response_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    example_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiSettings(Base):
    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(16), default="openai")
    openai_key_enc: Mapped[str] = mapped_column(Text, default="")
    openai_admin_key_enc: Mapped[str] = mapped_column(Text, default="")
    openai_model: Mapped[str] = mapped_column(String(128), default="gpt-4o-mini")
    ollama_url: Mapped[str] = mapped_column(String(255), default="http://192.168.2.230:11440")
    ollama_model: Mapped[str] = mapped_column(String(128), default="qwen2.5:14b-instruct")


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailbox_connections.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource: Mapped[str | None] = mapped_column(String(512), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    mailbox: Mapped[MailboxConnection] = relationship(back_populates="webhook")
