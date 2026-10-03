"""Sign-in for the web app and admin management of sign-in accounts."""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.session import (
    SESSION_COOKIE,
    SESSION_DAYS,
    create_session,
    current_user_id,
    delete_session,
    forget_user_sessions,
    hash_password,
    normalize_email,
    require_admin,
    require_user,
    token_hash,
    verify_password,
)
from app.db import get_db
from app.models import MailboxConnection, User, UserSession

router = APIRouter(prefix="/api/session", tags=["session"])
users_router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(require_user)])

MIN_PASSWORD_LENGTH = 3


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class SessionUserOut(BaseModel):
    id: int
    email: str
    display_name: str | None
    is_admin: bool


class UserOut(SessionUserOut):
    mailbox_count: int
    created_at: datetime | None
    last_login_at: datetime | None


class UserCreateIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    display_name: str | None = Field(default=None, max_length=255)
    is_admin: bool = False


class UserUpdateIn(BaseModel):
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=256)
    display_name: str | None = Field(default=None, max_length=255)
    is_admin: bool | None = None


def _session_out(user: User) -> SessionUserOut:
    return SessionUserOut(id=user.id, email=user.email or "", display_name=user.display_name, is_admin=bool(user.is_admin))


def _valid_email(value: str) -> str:
    email = normalize_email(value)
    local, _, domain = email.partition("@")
    if not local or "." not in domain or " " in email:
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    return email


@router.post("/login", response_model=SessionUserOut)
async def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> SessionUserOut:
    user = db.query(User).filter(User.email == normalize_email(payload.email)).one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        await asyncio.sleep(0.6)
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    token, expires_at = create_session(db, user)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        expires=expires_at,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return _session_out(user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    delete_session(db, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=SessionUserOut)
def me(user_id: int = Depends(require_user), db: Session = Depends(get_db)) -> SessionUserOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    return _session_out(user)


@router.post("/password")
def change_own_password(
    payload: PasswordChangeIn,
    request: Request,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, user_id)
    if user is None or not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    db.query(UserSession).filter(
        UserSession.user_id == user.id,
        UserSession.token_hash != token_hash(request.cookies.get(SESSION_COOKIE) or ""),
    ).delete(synchronize_session=False)
    db.commit()
    forget_user_sessions(user.id)
    return {"ok": True}


def _user_out(db: Session, user: User) -> UserOut:
    count = (
        db.query(func.count(MailboxConnection.id))
        .filter(MailboxConnection.user_id == user.id, MailboxConnection.is_active.is_(True))
        .scalar()
        or 0
    )
    return UserOut(
        id=user.id,
        email=user.email or "",
        display_name=user.display_name,
        is_admin=bool(user.is_admin),
        mailbox_count=int(count),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@users_router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[UserOut]:
    require_admin()
    users = db.query(User).filter(User.email.is_not(None)).order_by(User.id).all()
    return [_user_out(db, user) for user in users]


@users_router.post("", response_model=UserOut, status_code=201)
def create_user(payload: UserCreateIn, db: Session = Depends(get_db)) -> UserOut:
    require_admin()
    email = _valid_email(payload.email)
    if db.query(User.id).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="A sign-in account with this email already exists")
    user = User(
        external_id=f"user:{secrets.token_hex(8)}",
        email=email,
        password_hash=hash_password(payload.password),
        display_name=(payload.display_name or "").strip() or None,
        is_admin=payload.is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_out(db, user)


def _admin_count(db: Session) -> int:
    return int(db.query(func.count(User.id)).filter(User.is_admin.is_(True), User.email.is_not(None)).scalar() or 0)


@users_router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdateIn, db: Session = Depends(get_db)) -> UserOut:
    require_admin()
    user = db.get(User, user_id)
    if user is None or user.email is None:
        raise HTTPException(status_code=404, detail="Sign-in account not found")
    fields = payload.model_fields_set
    if "display_name" in fields:
        user.display_name = (payload.display_name or "").strip() or None
    if "is_admin" in fields and payload.is_admin is not None and payload.is_admin != user.is_admin:
        if not payload.is_admin and _admin_count(db) <= 1:
            raise HTTPException(status_code=409, detail="Keep at least one admin sign-in account")
        user.is_admin = payload.is_admin
        forget_user_sessions(user.id)
    if "password" in fields and payload.password:
        user.password_hash = hash_password(payload.password)
        if user.id != current_user_id.get():
            db.query(UserSession).filter(UserSession.user_id == user.id).delete(synchronize_session=False)
            forget_user_sessions(user.id)
    db.commit()
    db.refresh(user)
    return _user_out(db, user)


@users_router.delete("/{user_id}")
async def delete_user(user_id: int, db: Session = Depends(get_db)) -> dict:
    require_admin()
    if user_id == current_user_id.get():
        raise HTTPException(status_code=409, detail="You cannot delete the account you are signed in with")
    user = db.get(User, user_id)
    if user is None or user.email is None:
        raise HTTPException(status_code=404, detail="Sign-in account not found")
    if user.is_admin and _admin_count(db) <= 1:
        raise HTTPException(status_code=409, detail="Keep at least one admin sign-in account")

    from app.api.mailboxes import disconnect_mailbox_record

    removed = 0
    for mailbox in db.query(MailboxConnection).filter(MailboxConnection.user_id == user.id).all():
        if mailbox.is_active:
            await disconnect_mailbox_record(db, mailbox)
            removed += 1
    # Inactive mailbox rows keep the foreign key; hand them to the deleting admin so the
    # user row can go. They hold no mail (disconnect deletes it) and are invisible.
    db.query(MailboxConnection).filter(MailboxConnection.user_id == user.id).update(
        {MailboxConnection.user_id: current_user_id.get()}, synchronize_session=False
    )
    db.query(UserSession).filter(UserSession.user_id == user.id).delete(synchronize_session=False)
    forget_user_sessions(user.id)
    db.delete(user)
    db.commit()
    return {"ok": True, "removed_mailboxes": removed}
