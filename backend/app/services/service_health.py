from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.db import SessionLocal
from app.models import ServiceHeartbeat

logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL_SECONDS = 15
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"
STARTED_AT = datetime.now(timezone.utc)

# Self-reported health for the components running inside this process. A worker
# can keep writing heartbeats (liveness) while one of its consume loops has
# silently stalled, so each component reports its own state and the heartbeat
# persists the worst of them. An empty registry means every component is
# healthy. The event lets the heartbeat loop persist a change at once rather
# than waiting out its interval, so the monitoring page reflects a stall or a
# recovery promptly.
_HEALTH_PRIORITY = {"healthy": 0, "recovering": 1, "offline": 2}
_component_health_lock = threading.Lock()
_component_health: dict[str, str] = {}
_health_changed: asyncio.Event | None = None


def _notify_health_changed() -> None:
    event = _health_changed
    loop = _health_loop
    if event is None or loop is None:
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        # The loop is closing; the next heartbeat tick still carries the state.
        pass


_health_loop: asyncio.AbstractEventLoop | None = None


def set_component_health(component: str, status: str) -> None:
    """Record one component's health; ``healthy`` clears it from the registry."""
    with _component_health_lock:
        current = _component_health.get(component, "healthy")
        if status == "healthy":
            if component not in _component_health:
                return
            _component_health.pop(component, None)
        else:
            if current == status:
                return
            _component_health[component] = status
    _notify_health_changed()


def worker_health_status() -> str:
    """The worst self-reported component state, or ``healthy`` when none degraded."""
    with _component_health_lock:
        if not _component_health:
            return "healthy"
        return max(_component_health.values(), key=lambda value: _HEALTH_PRIORITY.get(value, 1))


def record_service_heartbeat(service_name: str) -> None:
    now = datetime.now(timezone.utc)
    status = worker_health_status()
    with SessionLocal() as db:
        row = db.query(ServiceHeartbeat).filter(
            ServiceHeartbeat.service_name == service_name,
            ServiceHeartbeat.instance_id == INSTANCE_ID,
        ).one_or_none()
        if row is None:
            row = ServiceHeartbeat(
                service_name=service_name,
                instance_id=INSTANCE_ID,
                started_at=STARTED_AT,
                last_seen_at=now,
                status=status,
            )
            db.add(row)
        else:
            row.last_seen_at = now
            row.status = status
        db.commit()


async def service_heartbeat_loop(
    stop_event: asyncio.Event,
    service_name: str,
    *,
    heartbeat_path: Path | None = None,
) -> None:
    global _health_changed, _health_loop
    _health_changed = asyncio.Event()
    _health_loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        if heartbeat_path is not None:
            heartbeat_path.write_text("")
        try:
            await asyncio.to_thread(record_service_heartbeat, service_name)
        except Exception:
            # Liveness must keep running through a temporary database outage;
            # the monitoring page will correctly show this replica as stale.
            logger.exception("Could not persist %s heartbeat", service_name)
        _health_changed.clear()
        # Wake on shutdown, on a component-health change, or after the interval.
        waiters = [asyncio.create_task(stop_event.wait()), asyncio.create_task(_health_changed.wait())]
        try:
            done, pending = await asyncio.wait(
                waiters, timeout=HEARTBEAT_INTERVAL_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in waiters:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
