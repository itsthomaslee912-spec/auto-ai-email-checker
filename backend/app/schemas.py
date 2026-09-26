from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer


def _as_utc(value: Any) -> Any:
    if value is None or not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = _as_utc(value)
    return aware.isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    BeforeValidator(_as_utc),
    PlainSerializer(_json_utc, when_used="json"),
]

EmailLabelLiteral = Literal[
    "application_confirmation",
    "application_action_required",
    "screening",
    "assessment",
    "interview_invitation",
    "interview_scheduled",
    "interview_follow_up",
    "offer",
    "rejected_closed",
    "recruitment_alert",
    "other",
]
ProviderLiteral = Literal["google", "microsoft"]


class MailboxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: ProviderLiteral
    email_address: str
    display_name: str | None = None
    is_active: bool
    created_at: UtcDateTime | None = None


class EmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mailbox_id: int
    provider_message_id: str
    subject: str
    sender: str
    received_at: UtcDateTime | None = None
    snippet: str
    label: str
    interview_subtype: str | None = None
    confidence: float | None = None
    is_read: bool = False
    human_corrected: bool = False
    folder: str = "inbox"
    created_at: UtcDateTime | None = None


class EmailPageOut(BaseModel):
    items: list[EmailOut]
    next_cursor: int | None = None
    next_received_at: UtcDateTime | None = None
    next_is_read: bool | None = None
    has_more: bool = False
    total: int = 0
    label_counts: dict[str, int]
    mailbox_counts: dict[str, int]
    mailbox_unread_counts: dict[str, int] = {}
    folder_counts: dict[str, int] = {}


class MarkAllReadOut(BaseModel):
    marked: int = 0


class EmailDetailOut(EmailOut):
    body_text: str = ""
    body_html: str = ""


class EmailAttachmentIn(BaseModel):
    name: str
    content_type: str = "application/octet-stream"
    content_base64: str


class SendEmailIn(BaseModel):
    mailbox_id: int
    to_address: str
    subject: str
    body_text: str
    attachments: list[EmailAttachmentIn] = []


class SendEmailOut(BaseModel):
    ok: bool
    provider_message_id: str | None = None


class AiReplyOut(BaseModel):
    body_text: str


class HealthOut(BaseModel):
    status: str
    service: str = "auto-ai-email-checker"


class ClassificationResult(BaseModel):
    label: EmailLabelLiteral
    interview_subtype: Literal["confirmation", "calendar_invite", "reminder", "reschedule", "time_change", "cancellation"] | None = None
    confidence: float | None = None
    response_id: str | None = None


class EmailLabelUpdateIn(BaseModel):
    label: EmailLabelLiteral
    save_training: bool = True
    interview_subtype: Literal["confirmation", "calendar_invite", "reminder", "reschedule", "time_change", "cancellation"] | None = None


class ClassifyPromptStatusOut(BaseModel):
    unused_count: int = 0
    active_version_id: int | None = None
    active_source: str | None = None
    example_count: int = 0
    updated_at: UtcDateTime | None = None


class ClassifyPromptUpdateOut(BaseModel):
    ok: bool
    example_count: int = 0
    prompt_version_id: int | None = None
    message: str = ""


class ClassifyTrainingExampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email_id: int
    previous_label: str
    corrected_label: str
    previous_subtype: str | None = None
    corrected_subtype: str | None = None
    subject: str
    sender: str
    snippet: str
    used_in_prompt_version_id: int | None = None
    created_at: UtcDateTime | None = None


class ClassifyTrainingPageOut(BaseModel):
    items: list[ClassifyTrainingExampleOut]
    unused_count: int = 0
    total: int = 0


class MailboxLabelStatsOut(BaseModel):
    mailbox_id: int | None = None
    date_from: str
    date_to: str
    total: int = 0
    label_counts: dict[str, int]


class LabelTimelineBucketOut(BaseModel):
    bucket: str
    count: int = 0


class LabelTimelineOut(BaseModel):
    mailbox_id: int
    label: str
    bucket: str
    date_from: str
    date_to: str
    buckets: list[LabelTimelineBucketOut]


class OutcomeEntryOut(BaseModel):
    company: str = ""
    role: str = ""
    received_at: UtcDateTime | None = None
    subject: str = ""


class MailboxOutcomesOut(BaseModel):
    mailbox_id: int
    date_from: str
    date_to: str
    application_confirmation: list[OutcomeEntryOut] = []
    rejected_closed: list[OutcomeEntryOut] = []
    screening: list[OutcomeEntryOut] = []
    interview_scheduled: list[OutcomeEntryOut] = []
    items: list[OutcomeEntryOut] = []
    total: int = 0
    label: str | None = None
