"""Transactional-outbox relay service."""
from __future__ import annotations

import asyncio
import logging

from app.services.outbox_publisher import outbox_publisher_loop
from app.workers.runtime import run_service


async def run() -> None:
    await run_service("event-relay", (outbox_publisher_loop,))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
