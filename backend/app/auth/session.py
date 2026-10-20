"""Sign-in accounts for the web app: password hashing, cookie sessions, and per-request scoping.

Every protected request runs with ``current_user_id`` set, and mailbox queries
use ``mailbox_scope()`` so one sign-in account never sees another's mail.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import MailboxConnection, User, UserSession

SESSION_COOKIE = "aec_session"
SESSION_DAYS = 30
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)
current_user_is_admin: ContextVar[bool] = ContextVar("current_user_is_admin", default=False)

# token_hash -> (user_id, is_admin, session_expires_ts, cached_until_ts)
_session_cache: dict[str, tuple[int, bool, float, float]] = {}
_CACHE_SECONDS = 30.0


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str | None) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.add(UserSession(user_id=user.id, token_hash=token_hash(token), expires_at=expires_at))
    db.query(UserSession).filter(UserSession.expires_at < datetime.now(timezone.utc)).delete(
        synchronize_session=False
    )
    db.commit()
    return token, expires_at


def delete_session(db: Session, token: str | None) -> None:
    if not token:
        return
    hashed = token_hash(token)
    _session_cache.pop(hashed, None)
    db.query(UserSession).filter(UserSession.token_hash == hashed).delete(synchronize_session=False)
    db.commit()


def forget_user_sessions(user_id: int) -> None:
    for key, value in list(_session_cache.items()):
        if value[0] == user_id:
            _session_cache.pop(key, None)


def _lookup(token: str) -> tuple[int, bool] | None:
    hashed = token_hash(token)
    now = time.time()
    cached = _session_cache.get(hashed)
    if cached and cached[3] > now and cached[2] > now:
        return cached[0], cached[1]
    db = SessionLocal()
    try:
        row = (
            db.query(UserSession.user_id, UserSession.expires_at, User.is_admin)
            .join(User, User.id == UserSession.user_id)
            .filter(UserSession.token_hash == hashed)
            .one_or_none()
        )
    finally:
        db.close()
    if row is None:
        _session_cache.pop(hashed, None)
        return None
    user_id, expires_at, is_admin = row
    expires_ts = expires_at.timestamp()
    if expires_ts <= now:
        return None
    _session_cache[hashed] = (int(user_id), bool(is_admin), expires_ts, now + _CACHE_SECONDS)
    if len(_session_cache) > 512:
        _session_cache.clear()
    return int(user_id), bool(is_admin)


def session_user(request: Request) -> tuple[int, bool] | None:
    token = request.cookies.get(SESSION_COOKIE)
    return _lookup(token) if token else None


def session_token_user(token: str | None) -> tuple[int, bool] | None:
    """Authenticate transports such as WebSockets that do not use Request."""
    return _lookup(token) if token else None


async def require_user(request: Request) -> int:
    """Router dependency: reject anonymous requests and scope the rest to the signed-in account."""
    found = session_user(request)
    if found is None:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    user_id, is_admin = found
    current_user_id.set(user_id)
    current_user_is_admin.set(is_admin)
    return user_id


def require_admin() -> None:
    if not current_user_is_admin.get():
        raise HTTPException(status_code=403, detail="Only an admin sign-in account can do this")


def mailbox_scope():
    """Filter for mailboxes visible to the current request (active and owned by the signed-in account)."""
    conditions = [MailboxConnection.is_active.is_(True)]
    user_id = current_user_id.get()
    if user_id is not None:
        conditions.append(MailboxConnection.user_id == user_id)
    return and_(*conditions)


class MailboxOwnedElsewhere(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            detail="This email account is already connected to another sign-in account.",
        )


def claim_mailbox_for_user(db: Session, mailbox: MailboxConnection, user_id: int) -> None:
    """Attach an existing mailbox row to ``user_id`` during reconnect.

    An active mailbox owned by someone else is refused. An inactive one changes
    hands with its leftover rows cleared so nothing from the previous owner shows up.
    """
    if mailbox.user_id == user_id:
        return
    if mailbox.is_active:
        raise MailboxOwnedElsewhere()
    from app.models import CalendarEvent, EmailMessage

    db.query(CalendarEvent).filter(CalendarEvent.mailbox_id == mailbox.id).delete(synchronize_session=False)
    db.query(EmailMessage).filter(EmailMessage.mailbox_id == mailbox.id).delete(synchronize_session=False)
    mailbox.user_id = user_id
    mailbox.sync_cursor = None
    mailbox.full_sync_completed = False


def owned_mailbox_ids(db: Session, user_id: int) -> set[int]:
    return {
        row[0]
        for row in db.query(MailboxConnection.id)
        .filter(MailboxConnection.is_active.is_(True), MailboxConnection.user_id == user_id)
        .all()
    }
