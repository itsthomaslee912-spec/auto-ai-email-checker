"""Standalone background-worker entry point for container deployments."""
from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from app.db import init_db

HEARTBEAT_PATH = Path("/tmp/worker-heartbeat")
HEARTBEAT_INTERVAL_SECONDS = 15


async def heartbeat_loop(stop_event: asyncio.Event) -> None:
    """Touch a file on an interval so the container healthcheck can detect a hung event loop."""
    while not stop_event.is_set():
        HEARTBEAT_PATH.write_text("")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            pass
from app.realtime.renewal import renewal_loop
from app.services.auto_sync import auto_sync_loop
from app.services.calendar_backfill import backfill_calendar_events
from app.services.outbox_publisher import outbox_publisher_loop
from app.services.email_event_consumer import email_event_consumer_loop
from app.services.outcome_backfill import backfill_outcomes
from app.services.reclassify import start_all_pending_reclassify
from app.services.webhook_queue import webhook_queue_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def run() -> None:
    init_db()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

    async def backfills() -> None:
        await backfill_outcomes(stop_event)
        if not stop_event.is_set():
            await backfill_calendar_events(stop_event)

    async def pending_after_warmup() -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15)
        except TimeoutError:
            await start_all_pending_reclassify()

    tasks = [
        asyncio.create_task(heartbeat_loop(stop_event)),
        asyncio.create_task(renewal_loop(stop_event)),
        asyncio.create_task(auto_sync_loop(stop_event)),
        asyncio.create_task(webhook_queue_loop(stop_event)),
        asyncio.create_task(outbox_publisher_loop(stop_event)),
        asyncio.create_task(email_event_consumer_loop(stop_event)),
        asyncio.create_task(backfills()),
        asyncio.create_task(pending_after_warmup()),
    ]
    logger.info("Email worker started")
    try:
        await stop_event.wait()
    finally:
        stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Email worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
