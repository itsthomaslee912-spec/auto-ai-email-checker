from __future__ import annotations

import asyncio

from app.db import SessionLocal
from app.models import MailboxConnection
from app.services.webhook_registration import register_mailbox_webhook


async def main() -> None:
    with SessionLocal() as db:
        mailboxes = (
            db.query(MailboxConnection)
            .filter(MailboxConnection.is_active.is_(True))
            .all()
        )
        if not mailboxes:
            print("No active mailboxes found.")
            return

        failures = 0
        for mailbox in mailboxes:
            try:
                webhook = await register_mailbox_webhook(db, mailbox)
                print(
                    f"Registered {mailbox.provider} webhook for {mailbox.email_address}; "
                    f"expires {webhook.expires_at.isoformat() if webhook.expires_at else 'unknown'}"
                )
            except Exception as exc:
                failures += 1
                detail = getattr(exc, "detail", None) or str(exc)
                print(f"Failed {mailbox.email_address}: {detail}")

        if failures:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
