from __future__ import annotations

from app.models import MailFolder

VALID_FOLDERS = {item.value for item in MailFolder}

OUTLOOK_WELL_KNOWN = {
    "inbox": MailFolder.INBOX.value,
    "junkemail": MailFolder.SPAM.value,
    "junk": MailFolder.SPAM.value,
    "spam": MailFolder.SPAM.value,
    "deleteditems": MailFolder.TRASH.value,
    "deleted": MailFolder.TRASH.value,
    "trash": MailFolder.TRASH.value,
    "archive": MailFolder.ARCHIVE.value,
}


def normalize_folder(raw: str | None) -> str:
    label = (raw or "").strip().lower()
    if label in VALID_FOLDERS:
        return label
    return MailFolder.INBOX.value


def folder_from_gmail_labels(label_ids: list | None) -> str:
    labels = {str(item).upper() for item in (label_ids or [])}
    if "TRASH" in labels:
        return MailFolder.TRASH.value
    if "SPAM" in labels:
        return MailFolder.SPAM.value
    if "SENT" in labels:
        return MailFolder.SENT.value
    if "INBOX" in labels:
        return MailFolder.INBOX.value
    return MailFolder.ARCHIVE.value


def folder_from_outlook_well_known(name: str | None) -> str:
    key = (name or "").strip().lower().replace(" ", "")
    return OUTLOOK_WELL_KNOWN.get(key, MailFolder.INBOX.value)
