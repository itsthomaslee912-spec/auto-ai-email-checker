from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.oauth_google import (
    exchange_google_code,
    fetch_google_userinfo,
    google_auth_url,
    upsert_google_mailbox,
)
from app.auth.oauth_microsoft import (
    exchange_microsoft_code,
    fetch_microsoft_profile,
    microsoft_auth_url,
    upsert_microsoft_mailbox,
)
from app.auth.pending_oauth import pop_pending, put_pending
from app.config import get_settings
from app.db import get_db
from app.services.mailbox_sync import start_mailbox_sync
from app.services.webhook_registration import register_mailbox_webhook

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.get("/google/start")
def google_start(
    email: str = Query(..., min_length=3, description="Gmail address to connect"),
    name: str | None = Query(default=None, description="Optional display name"),
) -> RedirectResponse:
    email_clean = email.strip().lower()
    if "@" not in email_clean:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    display_name = (name or "").strip() or email_clean.split("@", 1)[0]
    state = secrets.token_urlsafe(24)
    put_pending(state, provider="google", email=email_clean, name=display_name)
    return RedirectResponse(google_auth_url(state, login_hint=email_clean))


@router.get("/google/callback")
async def google_callback(
    background_tasks: BackgroundTasks,
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    settings = get_settings()
    pending = pop_pending(state)
    if error:
        return RedirectResponse(f"{settings.frontend_origin}/?oauth=error&provider=google&detail={error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

    token_payload = await exchange_google_code(code)
    userinfo = await fetch_google_userinfo(token_payload["access_token"])
    google_email = (userinfo.get("email") or "").strip().lower()

    if pending and pending.email and google_email != pending.email:
        qs = urlencode(
            {
                "oauth": "error",
                "provider": "google",
                "detail": (
                    f"Signed in as {google_email}, but you entered {pending.email}. "
                    "Use the same Gmail account."
                ),
            }
        )
        return RedirectResponse(f"{settings.frontend_origin}/?{qs}")

    preferred_name = pending.name if pending else None
    mailbox = upsert_google_mailbox(
        db,
        token_payload=token_payload,
        userinfo=userinfo,
        preferred_name=preferred_name,
    )
    webhook_state, webhook_detail = await _register_connected_mailbox(
        db, mailbox, token_payload.get("access_token")
    )
    background_tasks.add_task(
        _kickoff_sync, mailbox.id, register_webhook=webhook_state != "active"
    )
    qs = urlencode(
        {
            "oauth": "ok",
            "provider": "google",
            "email": mailbox.email_address,
            "name": mailbox.display_name or "",
            "webhook": webhook_state,
            "detail": webhook_detail,
        }
    )
    return RedirectResponse(f"{settings.frontend_origin}/?{qs}")


@router.get("/microsoft/start")
def microsoft_start(
    email: str = Query(..., min_length=3),
    name: str | None = Query(default=None),
) -> RedirectResponse:
    email_clean = email.strip().lower()
    if "@" not in email_clean:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    display_name = (name or "").strip() or email_clean.split("@", 1)[0]
    state = secrets.token_urlsafe(24)
    put_pending(state, provider="microsoft", email=email_clean, name=display_name)
    return RedirectResponse(microsoft_auth_url(state, login_hint=email_clean))


@router.get("/microsoft/callback")
async def microsoft_callback(
    background_tasks: BackgroundTasks,
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    settings = get_settings()
    pending = pop_pending(state)
    if error:
        return RedirectResponse(f"{settings.frontend_origin}/?oauth=error&provider=microsoft&detail={error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

    token_payload = await exchange_microsoft_code(code)
    profile = await fetch_microsoft_profile(token_payload["access_token"])
    ms_email = (profile.get("mail") or profile.get("userPrincipalName") or "").strip().lower()

    if pending and pending.email and ms_email != pending.email:
        qs = urlencode(
            {
                "oauth": "error",
                "provider": "microsoft",
                "detail": (
                    f"Signed in as {ms_email}, but you entered {pending.email}. "
                    "Use the same Outlook account."
                ),
            }
        )
        return RedirectResponse(f"{settings.frontend_origin}/?{qs}")

    mailbox = upsert_microsoft_mailbox(
        db,
        token_payload=token_payload,
        profile=profile,
        preferred_name=pending.name if pending else None,
    )
    webhook_state, webhook_detail = await _register_connected_mailbox(
        db, mailbox, token_payload.get("access_token")
    )
    background_tasks.add_task(
        _kickoff_sync, mailbox.id, register_webhook=webhook_state != "active"
    )
    qs = urlencode(
        {
            "oauth": "ok",
            "provider": "microsoft",
            "email": mailbox.email_address,
            "name": mailbox.display_name or "",
            "webhook": webhook_state,
            "detail": webhook_detail,
        }
    )
    return RedirectResponse(f"{settings.frontend_origin}/?{qs}")


async def _register_connected_mailbox(
    db: Session, mailbox, access_token: str | None
) -> tuple[str, str]:
    try:
        await register_mailbox_webhook(db, mailbox, access_token=access_token)
        return "active", ""
    except Exception as exc:
        logger.exception(
            "Immediate webhook registration failed for mailbox %s", mailbox.id
        )
        detail = getattr(exc, "detail", None) or str(exc) or "Webhook registration failed"
        return "fallback", str(detail)


async def _kickoff_sync(mailbox_id: int, *, register_webhook: bool = True) -> None:
    await start_mailbox_sync(mailbox_id, register_webhook=register_webhook)
