"""Rewrite email_messages.received_at from Gmail internalDate (UTC).

Usage (from backend/):
  .venv\\Scripts\\python.exe -m scripts.repair_received_at
  .venv\\Scripts\\python.exe -m scripts.repair_received_at --mailbox-id 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python -m scripts.repair_received_at` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.oauth_google import ensure_google_access_token
from app.db import SessionLocal, init_db
from app.email.gmail import gmail_get_messages
from app.models import EmailMessage, MailboxConnection, Provider
from app.timeutil import as_utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("repair_received_at")

# Stay under Gmail user-rate quota (previous run hit 403s at concurrency 4).
CONCURRENCY = 2
CHUNK = 20
PAUSE_S = 0.35


async def repair_mailbox(mailbox_id: int) -> tuple[int, int]:
    db = SessionLocal()
    try:
        mailbox = (
            db.query(MailboxConnection)
            .filter(
                MailboxConnection.id == mailbox_id,
                MailboxConnection.is_active.is_(True),
                MailboxConnection.provider == Provider.GOOGLE.value,
            )
            .one_or_none()
        )
        if mailbox is None:
            logger.warning("Mailbox %s not found / inactive / not Google", mailbox_id)
            return 0, 0

        rows = (
            db.query(EmailMessage.id, EmailMessage.provider_message_id)
            .filter(EmailMessage.mailbox_id == mailbox.id)
            .all()
        )
        if not rows:
            mailbox.received_at_utc_fixed = True
            db.commit()
            return 0, 0

        id_by_provider = {pid: eid for eid, pid in rows}
        provider_ids = list(id_by_provider.keys())
        total = len(provider_ids)
        updated = 0
        logger.info("Repairing mailbox=%s email=%s total=%s", mailbox.id, mailbox.email_address, total)

        for start in range(0, total, CHUNK):
            token = await ensure_google_access_token(db, mailbox)
            chunk = provider_ids[start : start + CHUNK]
            raws = await gmail_get_messages(
                token, chunk, concurrency=CONCURRENCY, format="minimal"
            )
            for raw in raws:
                mid = raw.get("id")
                if not mid or mid not in id_by_provider:
                    continue
                internal = raw.get("internalDate")
                if not internal:
                    continue
                try:
                    received_at = as_utc(
                        datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
                    )
                except Exception:
                    continue
                row = db.get(EmailMessage, id_by_provider[mid])
                if row is None:
                    continue
                if row.received_at != received_at:
                    row.received_at = received_at
                    updated += 1
            db.commit()
            done = min(start + CHUNK, total)
            if done % 200 < CHUNK or done == total:
                logger.info(
                    "mailbox=%s progress %s/%s updated=%s",
                    mailbox.id,
                    done,
                    total,
                    updated,
                )
            if PAUSE_S > 0 and start + CHUNK < total:
                await asyncio.sleep(PAUSE_S)

        mailbox.received_at_utc_fixed = True
        db.commit()
        logger.info(
            "Finished mailbox=%s updated=%s total=%s",
            mailbox.id,
            updated,
            total,
        )
        return updated, total
    finally:
        db.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mailbox-id", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Re-repair even if already fixed")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        q = db.query(MailboxConnection).filter(
            MailboxConnection.is_active.is_(True),
            MailboxConnection.provider == Provider.GOOGLE.value,
        )
        if args.mailbox_id is not None:
            q = q.filter(MailboxConnection.id == args.mailbox_id)
        elif not args.force:
            q = q.filter(MailboxConnection.received_at_utc_fixed.is_(False))
        mailbox_ids = [row.id for row in q.all()]
    finally:
        db.close()

    if not mailbox_ids:
        logger.info("Nothing to repair")
        return

    for mid in mailbox_ids:
        await repair_mailbox(mid)
    print("REPAIR_DONE")


if __name__ == "__main__":
    asyncio.run(main())
