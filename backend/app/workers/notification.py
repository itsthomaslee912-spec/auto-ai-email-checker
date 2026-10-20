"""Notification service: persist and route user-facing activity."""
from __future__ import annotations

import asyncio
import logging

from app.services.notification_consumer import notification_consumer_loop
from app.workers.runtime import run_service


async def run() -> None:
    await run_service("notification-service", (notification_consumer_loop,))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
