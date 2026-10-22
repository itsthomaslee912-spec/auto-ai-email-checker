from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from app.config import get_settings
from app.db import SessionLocal
from app.services.event_bus import event_envelope, persist_activity

_subscribers: set[asyncio.Queue[str]] = set()


async def subscribe() -> AsyncIterator[str]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2000)
    _subscribers.add(queue)
    try:
        yield "connected"
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(),
                    timeout=max(5, get_settings().websocket_heartbeat_seconds),
                )
            except TimeoutError:
                item = json.dumps({"event": "heartbeat", "data": {}})
            yield item
    finally:
        _subscribers.discard(queue)


async def publish(event: str, data: dict[str, Any]) -> None:
    def persist() -> dict[str, Any]:
        with SessionLocal() as db:
            try:
                envelope = persist_activity(db, event, data)
                db.commit()
                return envelope
            except Exception:
                # Realtime delivery must remain available during migrations,
                # database failover, and isolated tests whose dependency-
                # overridden database intentionally has no activity tables.
                db.rollback()
                return event_envelope(event, data, user_id=data.get("user_id"))

    envelope = await asyncio.to_thread(persist)
    await broadcast_envelope({"event": event, "data": data, **envelope})


async def broadcast_envelope(envelope: dict[str, Any]) -> None:
    """Fan out an already-persisted envelope inside this API process."""
    # Calendar changes can originate in this API process or arrive from a
    # worker through RabbitMQ.  In both cases the API replica that serves the
    # next calendar request must discard its process-local pending-interview
    # snapshot before notifying the browser to refetch it.
    if envelope.get("event") == "calendar.changed":
        from app.services.pending_interviews import invalidate_calendar_read_cache

        invalidate_calendar_read_cache()
    payload = json.dumps(envelope, default=str)
    dead: list[asyncio.Queue[str]] = []
    for queue in list(_subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(payload)
            except Exception:
                dead.append(queue)
    for queue in dead:
        _subscribers.discard(queue)
