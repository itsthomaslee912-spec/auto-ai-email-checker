from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.auth.session import owned_mailbox_ids, require_user
from app.db import SessionLocal
from app.realtime.sse import subscribe

router = APIRouter(tags=["events"])

_OWNERSHIP_REFRESH_SECONDS = 5.0


@router.get("/api/events")
async def events_stream(user_id: int = Depends(require_user)) -> StreamingResponse:
    def load_owned() -> set[int]:
        db = SessionLocal()
        try:
            return owned_mailbox_ids(db, user_id)
        finally:
            db.close()

    async def event_generator():
        owned = await asyncio.to_thread(load_owned)
        refreshed_at = time.monotonic()
        async for item in subscribe():
            if item == "connected":
                yield "event: connected\ndata: {}\n\n"
                continue
            try:
                payload = json.loads(item)
                event_name = payload.get("event") or "message"
                data = payload.get("data") or {}
                mailbox_id = data.get("mailbox_id") if isinstance(data, dict) else None
                if mailbox_id is not None and int(mailbox_id) not in owned:
                    # A mailbox connected after this stream opened is unknown until refreshed.
                    if time.monotonic() - refreshed_at < _OWNERSHIP_REFRESH_SECONDS:
                        continue
                    owned = await asyncio.to_thread(load_owned)
                    refreshed_at = time.monotonic()
                    if int(mailbox_id) not in owned:
                        continue
                yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
            except Exception:
                continue
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
