"""Provider integration service: webhooks, polling, and subscription renewal."""
from __future__ import annotations

import asyncio
import logging

from app.realtime.renewal import renewal_loop
from app.services.auto_sync import auto_sync_loop
from app.services.command_consumers import sync_command_loop
from app.services.leader_election import leader_elected_loops
from app.services.webhook_queue import webhook_queue_loop
from app.workers.runtime import run_service


async def _scheduler_leader_loop(stop_event: asyncio.Event) -> None:
    await leader_elected_loops(
        stop_event,
        "sync-schedulers",
        (renewal_loop, auto_sync_loop),
    )


async def run() -> None:
    await run_service(
        "sync-worker",
        (webhook_queue_loop, sync_command_loop, _scheduler_leader_loop),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
