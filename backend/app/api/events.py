from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.auth.session import SESSION_COOKIE, owned_mailbox_ids, require_user, session_token_user
from app.db import SessionLocal
from app.models import ActivityEvent
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


@router.get("/api/activity")
def activity_after(
    after_sequence: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    user_id: int = Depends(require_user),
) -> dict:
    """Durable events missed while a browser WebSocket was disconnected."""
    with SessionLocal() as db:
        rows = list(db.scalars(
            select(ActivityEvent)
            .where(ActivityEvent.user_id == user_id, ActivityEvent.id > after_sequence)
            .order_by(ActivityEvent.id)
            .limit(limit)
        ))
        return {
            "items": [
                {
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "sequence": row.id,
                    "occurred_at": row.created_at.isoformat() if row.created_at else None,
                    "data": json.loads(row.payload),
                }
                for row in rows
            ],
            "last_sequence": rows[-1].id if rows else after_sequence,
        }


@router.websocket("/api/ws/activity")
async def activity_socket(websocket: WebSocket) -> None:
    """Authenticated realtime stream with per-user mailbox filtering."""
    found = session_token_user(websocket.cookies.get(SESSION_COOKIE))
    if found is None:
        await websocket.close(code=4401, reason="Sign in to continue")
        return
    user_id, _is_admin = found

    def load_owned() -> set[int]:
        with SessionLocal() as db:
            return owned_mailbox_ids(db, user_id)

    owned = await asyncio.to_thread(load_owned)
    await websocket.accept()
    await websocket.send_json({"event": "connected", "data": {}})
    try:
        async for item in subscribe():
            if item == "connected":
                continue
            payload = json.loads(item)
            data = payload.get("data") or {}
            event_user_id = payload.get("user_id")
            mailbox_id = data.get("mailbox_id") if isinstance(data, dict) else None
            if event_user_id is not None and int(event_user_id) != user_id:
                continue
            if event_user_id is None and mailbox_id is not None and int(mailbox_id) not in owned:
                owned = await asyncio.to_thread(load_owned)
                if int(mailbox_id) not in owned:
                    continue
            await websocket.send_json({
                "event": payload.get("event") or payload.get("event_type") or "message",
                "event_id": payload.get("event_id"),
                "sequence": payload.get("sequence"),
                "data": data,
            })
    except WebSocketDisconnect:
        return
