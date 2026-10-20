from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from app.db import SessionLocal
from app.models import ServiceHeartbeat

logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL_SECONDS = 15
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"
STARTED_AT = datetime.now(timezone.utc)


def record_service_heartbeat(service_name: str) -> None:
    now = datetime.now(timezone.utc)
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
            )
            db.add(row)
        else:
            row.last_seen_at = now
        db.commit()


async def service_heartbeat_loop(
    stop_event: asyncio.Event,
    service_name: str,
    *,
    heartbeat_path: Path | None = None,
) -> None:
    while not stop_event.is_set():
        if heartbeat_path is not None:
            heartbeat_path.write_text("")
        try:
            await asyncio.to_thread(record_service_heartbeat, service_name)
        except Exception:
            # Liveness must keep running through a temporary database outage;
            # the monitoring page will correctly show this replica as stale.
            logger.exception("Could not persist %s heartbeat", service_name)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            pass
