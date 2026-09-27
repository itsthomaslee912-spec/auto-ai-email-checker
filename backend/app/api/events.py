from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.realtime.sse import subscribe

router = APIRouter(tags=["events"])


@router.get("/api/events")
async def events_stream() -> StreamingResponse:
    async def event_generator():
        async for item in subscribe():
            if item == "connected":
                yield "event: connected\ndata: {}\n\n"
                continue
            try:
                payload = json.loads(item)
                event_name = payload.get("event") or "message"
                data = json.dumps(payload.get("data") or {})
                yield f"event: {event_name}\ndata: {data}\n\n"
            except Exception:
                yield f"data: {item}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
