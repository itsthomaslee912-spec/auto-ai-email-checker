from __future__ import annotations

import asyncio
import logging
import signal
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from app.db import init_db
from app.config import get_settings
from app.services.service_health import service_heartbeat_loop

WorkerLoop = Callable[[asyncio.Event], Awaitable[None]]


async def run_service(
    service_name: str,
    loops: Sequence[WorkerLoop],
    *,
    heartbeat_path: str | None = None,
) -> None:
    """Run one background service with a shared shutdown and health lifecycle."""
    settings = get_settings()
    # In the distributed local runtime the API owns schema initialization.
    # Starting every worker with DDL caused concurrent ALTER TABLE races and
    # could take the API offline. A standalone worker still initializes its
    # database for backwards-compatible development usage.
    if settings.run_db_init and not settings.distributed_runtime:
        init_db()
    stop_event = asyncio.Event()
    event_loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            event_loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    heartbeat = Path(heartbeat_path) if heartbeat_path else Path(tempfile.gettempdir()) / f"{service_name}-heartbeat"
    tasks = [
        asyncio.create_task(
            service_heartbeat_loop(stop_event, service_name, heartbeat_path=heartbeat),
            name=f"{service_name}:heartbeat",
        ),
        *(
            asyncio.create_task(loop(stop_event), name=f"{service_name}:{loop.__name__}")
            for loop in loops
        ),
    ]
    shutdown_task = asyncio.create_task(stop_event.wait(), name=f"{service_name}:shutdown")
    logger = logging.getLogger(service_name)
    logger.info("%s started", service_name)
    try:
        done, _pending = await asyncio.wait(
            [shutdown_task, *tasks], return_when=asyncio.FIRST_COMPLETED
        )
        if shutdown_task not in done:
            failed = next(iter(done))
            error = failed.exception()
            if error is not None:
                raise error
            raise RuntimeError(f"{failed.get_name()} exited unexpectedly")
    finally:
        stop_event.set()
        shutdown_task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("%s stopped", service_name)
