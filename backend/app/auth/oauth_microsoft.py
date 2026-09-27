from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt_token, encrypt_token
from app.models import MailboxConnection, Provider, User

MS_SCOPES = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/User.Read",
]


def _authority() -> str:
    tenant = get_settings().microsoft_tenant or "common"
    return f"https://login.microsoftonline.com/{tenant}"


def microsoft_auth_url(state: str, *, login_hint: str | None = None) -> str:
    settings = get_settings()
    if not settings.microsoft_client_id:
        raise HTTPException(status_code=500, detail="MICROSOFT_CLIENT_ID is not configured")
    params = {
        "client_id": settings.microsoft_client_id,
        "response_type": "code",
        "redirect_uri": settings.microsoft_redirect_uri,
        "response_mode": "query",
        "scope": " ".join(MS_SCOPES),
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{_authority()}/oauth2/v2.0/authorize?{urlencode(params)}"


async def exchange_microsoft_code(code: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_authority()}/oauth2/v2.0/token",
            data={
                "client_id": settings.microsoft_client_id,
                "client_secret": settings.microsoft_client_secret,
                "code": code,
                "redirect_uri": settings.microsoft_redirect_uri,
                "grant_type": "authorization_code",
                "scope": " ".join(MS_SCOPES),
            },
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Microsoft token exchange failed: {resp.text}")
        return resp.json()


async def refresh_microsoft_access_token(refresh_token: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_authority()}/oauth2/v2.0/token",
            data={
                "client_id": settings.microsoft_client_id,
                "client_secret": settings.microsoft_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": " ".join(MS_SCOPES),
            },
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Microsoft token refresh failed: {resp.text}")
        return resp.json()


async def fetch_microsoft_profile(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=f"Microsoft profile failed: {resp.text}")
        return resp.json()


def upsert_microsoft_mailbox(
    db: Session,
    *,
    token_payload: dict,
    profile: dict,
    preferred_name: str | None = None,
    default_user_external_id: str = "default",
) -> MailboxConnection:
    email = profile.get("mail") or profile.get("userPrincipalName")
    if not email:
        raise HTTPException(status_code=400, detail="Microsoft account email missing")

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
            MailboxConnection.provider == Provider.MICROSOFT.value,
            MailboxConnection.email_address == email,
        )
        .one_or_none()
    )
    if mailbox is None:
        mailbox = MailboxConnection(
            user_id=user.id,
            provider=Provider.MICROSOFT.value,
            email_address=email,
        )
        db.add(mailbox)

    mailbox.display_name = (preferred_name or "").strip() or profile.get("displayName")
    mailbox.provider_user_id = str(profile.get("id") or "")
    mailbox.access_token_enc = encrypt_token(access_token)
    if refresh_token:
        mailbox.refresh_token_enc = encrypt_token(refresh_token)
    mailbox.token_expires_at = expires_at
    mailbox.is_active = True
    db.commit()
    db.refresh(mailbox)
    return mailbox


async def ensure_microsoft_access_token(db: Session, mailbox: MailboxConnection) -> str:
    access = decrypt_token(mailbox.access_token_enc)
    expires = mailbox.token_expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires > datetime.now(timezone.utc) + timedelta(seconds=60):
        return access

    refresh = decrypt_token(mailbox.refresh_token_enc)
    if not refresh:
        raise HTTPException(status_code=401, detail="Microsoft refresh token missing; reconnect mailbox")

    payload = await refresh_microsoft_access_token(refresh)
    mailbox.access_token_enc = encrypt_token(payload["access_token"])
    expires_in = int(payload.get("expires_in", 3600))
    mailbox.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    if payload.get("refresh_token"):
        mailbox.refresh_token_enc = encrypt_token(payload["refresh_token"])
    db.commit()
    return payload["access_token"]
