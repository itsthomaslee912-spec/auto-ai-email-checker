from __future__ import annotations

import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import ClassifyCorrection, EmailMessage, MailboxConnection

logger = logging.getLogger(__name__)


def _delete_emails_by_ids(db: Session, email_ids: list[int]) -> int:
    if not email_ids:
        return 0
    db.query(ClassifyCorrection).filter(ClassifyCorrection.email_id.in_(email_ids)).delete(
        synchronize_session=False
    )
    return (
        db.query(EmailMessage)
        .filter(EmailMessage.id.in_(email_ids))
        .delete(synchronize_session=False)
    )


def delete_emails_for_mailbox(db: Session, mailbox_id: int) -> int:
    """Remove stored mail (and classify_corrections) for one mailbox."""
    email_ids = [
        row[0] for row in db.query(EmailMessage.id).filter(EmailMessage.mailbox_id == mailbox_id).all()
    ]
    deleted = _delete_emails_by_ids(db, email_ids)
    if deleted:
        logger.info("Deleted %s emails for mailbox %s", deleted, mailbox_id)
    return deleted


def purge_orphaned_emails(db: Session) -> int:
    """Delete mail whose mailbox is missing or disconnected (is_active=False)."""
    email_ids = [
        row[0]
        for row in db.query(EmailMessage.id)
        .outerjoin(MailboxConnection, MailboxConnection.id == EmailMessage.mailbox_id)
        .filter(or_(MailboxConnection.id.is_(None), MailboxConnection.is_active.is_(False)))
        .all()
    ]
    deleted = _delete_emails_by_ids(db, email_ids)
    if deleted:
        logger.info("Purged %s orphaned emails from disconnected or missing mailboxes", deleted)
    return deleted


def purge_orphaned_mailbox_mail() -> int:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        deleted = purge_orphaned_emails(db)
        db.commit()
        return deleted
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
