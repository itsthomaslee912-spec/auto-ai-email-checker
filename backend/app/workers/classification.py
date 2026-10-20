"""AI enrichment service: classification, extraction, and calendar projection."""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.services.calendar_backfill import backfill_calendar_events
from app.services.command_consumers import classification_command_loop
from app.services.email_event_consumer import email_event_consumer_loop
from app.services.leader_election import leader_elected_loops
from app.services.outcome_backfill import backfill_outcomes
from app.services.reclassify import start_all_pending_reclassify
from app.workers.runtime import run_service


async def _recovery_loop(stop_event: asyncio.Event) -> None:
    """Recover work committed before this service started.

    RabbitMQ handles the live path in production. Without a broker (mainly local
    development), the durable ``classification_pending`` flag is the queue and
    is scanned periodically so the sync service never performs AI work.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15)
            return
        except TimeoutError:
            await start_all_pending_reclassify()
        if get_settings().rabbitmq_enabled:
            await stop_event.wait()
            return


async def _backfill_loop(stop_event: asyncio.Event) -> None:
    await backfill_outcomes(stop_event)
    if not stop_event.is_set():
        await backfill_calendar_events(stop_event)


async def _maintenance_leader_loop(stop_event: asyncio.Event) -> None:
    await leader_elected_loops(
        stop_event,
        "classification-maintenance",
        (_recovery_loop, _backfill_loop),
    )


async def run() -> None:
    await run_service(
        "classification-worker",
        (email_event_consumer_loop, classification_command_loop, _maintenance_leader_loop),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
