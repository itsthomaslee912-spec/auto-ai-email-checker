from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence

from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.workers.runtime import WorkerLoop

logger = logging.getLogger(__name__)


def _lock_key(name: str) -> int:
    value = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")
    return value - (1 << 64) if value >= (1 << 63) else value


async def leader_elected_loops(
    stop_event: asyncio.Event,
    election_name: str,
    loops: Sequence[WorkerLoop],
) -> None:
    """Run singleton loops on one PostgreSQL-connected replica at a time.

    PostgreSQL session advisory locks provide automatic release when a process,
    container, node, or database connection disappears. The connection is
    checked periodically; losing it cancels the old leader's tasks before that
    replica competes for leadership again.
    """
    lock_key = _lock_key(f"auto-ai-email-checker:{election_name}")
    check_seconds = max(1.0, get_settings().leader_lease_check_seconds)

    while not stop_event.is_set():
        connection = None
        tasks: list[asyncio.Task] = []
        stop_task: asyncio.Task | None = None
        acquired = False
        try:
            connection = engine.connect()
            if connection.dialect.name == "postgresql":
                acquired = bool(connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
                ).scalar())
                # Session advisory locks survive commit. End the implicit
                # transaction so the elected worker does not remain "idle in
                # transaction" for its entire lifetime.
                connection.commit()
            else:
                # Local SQLite has one process and therefore no competing replica.
                acquired = True

            if not acquired:
                connection.close()
                connection = None
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=check_seconds)
                except TimeoutError:
                    pass
                continue

            logger.info("Replica became leader for %s", election_name)
            tasks = [
                asyncio.create_task(loop(stop_event), name=f"leader:{election_name}:{loop.__name__}")
                for loop in loops
            ]
            stop_task = asyncio.create_task(stop_event.wait(), name=f"leader:{election_name}:stop")

            while not stop_event.is_set():
                done, _ = await asyncio.wait(
                    [stop_task, *tasks],
                    timeout=check_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    return
                for task in list(tasks):
                    if not task.done():
                        continue
                    tasks.remove(task)
                    error = task.exception()
                    if error is not None:
                        raise error
                if not tasks:
                    raise RuntimeError(f"All leader tasks exited for {election_name}")
                connection.execute(text("SELECT 1"))
                connection.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Leader session lost for %s; tasks will be re-elected", election_name)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if stop_task is not None:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
            if connection is not None:
                if acquired and connection.dialect.name == "postgresql":
                    try:
                        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                        connection.commit()
                    except Exception:
                        pass
                connection.close()

        if not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=check_seconds)
            except TimeoutError:
                pass
