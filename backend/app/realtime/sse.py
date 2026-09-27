from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

_subscribers: set[asyncio.Queue[str]] = set()


async def subscribe() -> AsyncIterator[str]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2000)
    _subscribers.add(queue)
    try:
        yield "connected"
        while True:
            item = await queue.get()
            yield item
    finally:
        _subscribers.discard(queue)


async def publish(event: str, data: dict[str, Any]) -> None:
    payload = json.dumps({"event": event, "data": data}, default=str)
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
