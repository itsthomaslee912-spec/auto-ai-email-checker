from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt_token, encrypt_token
from app.models import MailboxConnection, Provider, User

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GMAIL_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def google_auth_url(state: str, *, login_hint: str | None = None) -> str:
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID is not configured")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent select_account",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_google_code(code: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Google token exchange failed: {resp.text}")
        return resp.json()


async def refresh_google_access_token(refresh_token: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Google token refresh failed: {resp.text}")
        return resp.json()


async def fetch_google_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Google userinfo failed: {resp.text}")
        return resp.json()


def upsert_google_mailbox(
    db: Session,
    *,
    token_payload: dict,
    userinfo: dict,
    preferred_name: str | None = None,
    default_user_external_id: str = "default",
) -> MailboxConnection:
    email = userinfo.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Google account email missing")

    user = db.query(User).filter(User.external_id == default_user_external_id).one_or_none()
    if user is None:
        user = User(external_id=default_user_external_id)
        db.add(user)
        db.flush()

    access_token = token_payload.get("access_token", "")
    refresh_token = token_payload.get("refresh_token", "")
    expires_in = int(token_payload.get("expires_in", 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    mailbox = (
        db.query(MailboxConnection)
        .filter(
            MailboxConnection.provider == Provider.GOOGLE.value,
            MailboxConnection.email_address == email,
        )
        .one_or_none()
    )
    if mailbox is None:
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.GOOGLE.value,
            email_address=email,
        )
        db.add(mailbox)

    mailbox.display_name = (preferred_name or "").strip() or userinfo.get("name")
    mailbox.provider_user_id = str(userinfo.get("id") or "")
    mailbox.access_token_enc = encrypt_token(access_token)
    if refresh_token:
        mailbox.refresh_token_enc = encrypt_token(refresh_token)
    mailbox.token_expires_at = expires_at
    mailbox.is_active = True
    db.commit()
    db.refresh(mailbox)
    return mailbox


async def ensure_google_access_token(db: Session, mailbox: MailboxConnection) -> str:
    access = decrypt_token(mailbox.access_token_enc)
    expires = mailbox.token_expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires > datetime.now(timezone.utc) + timedelta(seconds=60):
        return access

    refresh = decrypt_token(mailbox.refresh_token_enc)
    if not refresh:
        raise HTTPException(status_code=401, detail="Google refresh token missing; reconnect mailbox")

    payload = await refresh_google_access_token(refresh)
    mailbox.access_token_enc = encrypt_token(payload["access_token"])
    expires_in = int(payload.get("expires_in", 3600))
    mailbox.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    if payload.get("refresh_token"):
        mailbox.refresh_token_enc = encrypt_token(payload["refresh_token"])
    db.commit()
    return payload["access_token"]
