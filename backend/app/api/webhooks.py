from __future__ import annotations

import asyncio
import base64
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.services.mailbox_sync import process_gmail_notification, process_outlook_notification
from app.services.webhook_health import (
    record_webhook_delivery_failure,
    record_webhook_delivery_success,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
_gmail_pending: dict[str, str | None] = {}
_gmail_tasks: dict[str, asyncio.Task[None]] = {}
_MISSING = object()


async def _verify_gmail_push_auth(request: Request) -> None:
    settings = get_settings()
    audience = settings.gmail_pubsub_audience.strip()
    if not audience:
        raise HTTPException(
            status_code=503,
            detail="GMAIL_PUBSUB_AUDIENCE is not configured",
        )

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Missing Pub/Sub bearer token")

    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            token.strip(),
            google_requests.Request(),
            audience,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid Pub/Sub bearer token") from exc

    expected_email = settings.gmail_pubsub_service_account.strip().casefold()
    token_email = str(claims.get("email") or "").strip().casefold()
    if expected_email and token_email != expected_email:
        raise HTTPException(status_code=403, detail="Unexpected Pub/Sub service account")


@router.post("/gmail")
async def gmail_pubsub_push(request: Request) -> dict:
    try:
        await _verify_gmail_push_auth(request)
    except HTTPException as exc:
        record_webhook_delivery_failure("google", str(exc.detail))
        raise
    record_webhook_delivery_success("google")
    body = await request.json()
    message = body.get("message") or {}
    data_b64 = message.get("data")
    if not data_b64:
        return {"ok": True, "skipped": True}

    try:
        decoded = base64.b64decode(data_b64).decode("utf-8")
        payload = json.loads(decoded)
    except Exception:
        logger.exception("Invalid Gmail Pub/Sub payload")
        return {"ok": False}

    email_address = payload.get("emailAddress") or ""
    history_id = str(payload.get("historyId") or "") or None
    if not email_address:
        return {"ok": True, "skipped": True}
    _schedule_gmail(email_address, history_id)
    return {"ok": True}


def _schedule_gmail(email_address: str, history_id: str | None) -> None:
    """Coalesce Pub/Sub retries and acknowledge the HTTP request immediately."""
    _gmail_pending[email_address] = history_id
    task = _gmail_tasks.get(email_address)
    if task is None or task.done():
        _gmail_tasks[email_address] = asyncio.create_task(_drain_gmail(email_address))


async def _drain_gmail(email_address: str) -> None:
    try:
        while True:
            history_id = _gmail_pending.pop(email_address, _MISSING)
            if history_id is _MISSING:
                return
            await _handle_gmail(email_address, history_id)
    finally:
        _gmail_tasks.pop(email_address, None)
        if email_address in _gmail_pending:
            _schedule_gmail(email_address, _gmail_pending[email_address])


async def _handle_gmail(email_address: str, history_id: str | None) -> None:
    db = SessionLocal()
    try:
        await process_gmail_notification(db, email_address, history_id)
    except Exception:
        logger.exception("Gmail notification processing failed")
    finally:
        db.close()


@router.post("/outlook")
async def outlook_graph_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    # Graph validation handshake
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return Response(content=validation_token, media_type="text/plain", status_code=200)

    settings = get_settings()
    body = await request.json()
    notifications = body.get("value") or []
    for note in notifications:
        if note.get("clientState") != settings.microsoft_webhook_client_state:
            record_webhook_delivery_failure("microsoft", "Invalid Outlook clientState")
            logger.warning("Rejected Outlook notification with bad clientState")
            continue
        record_webhook_delivery_success("microsoft")
        subscription_id = note.get("subscriptionId") or ""
        resource_data = note.get("resourceData") or {}
        message_id = resource_data.get("id")
        if not message_id and note.get("resource"):
            # resource like Users/{id}/Messages/{id}
            parts = str(note["resource"]).rstrip("/").split("/")
            if parts:
                message_id = parts[-1]
        if subscription_id and message_id:
            background_tasks.add_task(_handle_outlook, subscription_id, message_id)

    return Response(status_code=202)


async def _handle_outlook(subscription_id: str, message_id: str) -> None:
    db = SessionLocal()
    try:
        await process_outlook_notification(db, subscription_id, message_id)
    except Exception:
        logger.exception("Outlook notification processing failed")
    finally:
        db.close()
