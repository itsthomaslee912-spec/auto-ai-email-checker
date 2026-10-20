from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.services.job_queue import claim, persist
from app.services.mailbox_sync import start_mailbox_sync
from app.services.reclassify import start_mailbox_reclassify

logger = logging.getLogger(__name__)


async def _wait(stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(
            stop_event.wait(),
            timeout=max(0.05, get_settings().command_queue_poll_seconds),
        )
    except TimeoutError:
        pass


async def sync_command_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        for mailbox_id, options in await asyncio.to_thread(claim, "sync"):
            try:
                status = await start_mailbox_sync(mailbox_id, **options)
                await asyncio.to_thread(persist, mailbox_id, "sync", status)
            except Exception as exc:
                logger.exception("Could not start queued sync for mailbox %s", mailbox_id)
                await asyncio.to_thread(persist, mailbox_id, "sync", {
                    "mailbox_id": mailbox_id,
                    "state": "error",
                    "message": str(exc)[:300],
                })
        await _wait(stop_event)


async def classification_command_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        for mailbox_id, options in await asyncio.to_thread(claim, "reclassify"):
            try:
                status = await start_mailbox_reclassify(mailbox_id, **options)
                await asyncio.to_thread(persist, mailbox_id, "reclassify", status)
            except Exception as exc:
                logger.exception("Could not start queued reclassify for mailbox %s", mailbox_id)
                await asyncio.to_thread(persist, mailbox_id, "reclassify", {
                    "mailbox_id": mailbox_id,
                    "state": "error",
                    "message": str(exc)[:300],
                })
        await _wait(stop_event)
