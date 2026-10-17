from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from app.config import get_settings
from app.services.webhook_health import (
    persist_webhook_delivery_failure,
    record_webhook_delivery_failure,
)
from app.services.webhook_queue import enqueue_webhook_delivery

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
_gmail_token_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_gmail_token_cache_lock = threading.Lock()


def _verify_gmail_token_cached(
    token: str, audience: str, expected_email: str,
) -> dict:
    """Verify a Pub/Sub token once and reuse it until shortly before expiry.

    Pub/Sub reuses bearer tokens across a burst. Fetching Google's certificates
    for every notification previously made acknowledgements take about a second
    each and amplified retry storms while the backend was recovering.
    """
    key = (token, audience, expected_email)
    now = time.time()
    with _gmail_token_cache_lock:
        cached = _gmail_token_cache.get(key)
        if cached is not None and cached[0] > now + 30:
            return cached[1]
        claims = id_token.verify_oauth2_token(
            token, google_requests.Request(), audience
        )
        expires_at = float(claims.get("exp") or now)
        if expires_at > now + 30:
            _gmail_token_cache[key] = (expires_at, claims)
        for cache_key, (expiry, _) in list(_gmail_token_cache.items()):
            if expiry <= now + 30:
                _gmail_token_cache.pop(cache_key, None)
        return claims


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
            _verify_gmail_token_cached,
            token.strip(),
            audience,
            settings.gmail_pubsub_service_account.strip().casefold(),
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
        persist_webhook_delivery_failure("google", str(exc.detail))
        raise
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
    queued = enqueue_webhook_delivery(
        "google",
        {"email_address": email_address, "history_id": history_id},
        # Gmail history is cumulative. Keep at most one queued job per account
        # and replace its payload with the newest history id during bursts.
        provider_id=email_address.strip().casefold(),
    )
    return {"ok": True, "queued": queued}


@router.post("/outlook")
async def outlook_graph_webhook(request: Request) -> Response:
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
            persist_webhook_delivery_failure("microsoft", "Invalid Outlook clientState")
            logger.warning("Rejected Outlook notification with bad clientState")
            continue
        subscription_id = note.get("subscriptionId") or ""
        resource_data = note.get("resourceData") or {}
        message_id = resource_data.get("id")
        if not message_id and note.get("resource"):
            # resource like Users/{id}/Messages/{id}
            parts = str(note["resource"]).rstrip("/").split("/")
            if parts:
                message_id = parts[-1]
        if subscription_id and message_id:
            enqueue_webhook_delivery(
                "microsoft",
                {"subscription_id": subscription_id, "message_id": message_id},
                provider_id=f"{subscription_id}:{message_id}",
            )

    return Response(status_code=202)
