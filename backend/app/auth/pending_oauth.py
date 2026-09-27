from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class PendingOAuth:
    provider: str
    email: str
    name: str
    created_at: float


_PENDING: dict[str, PendingOAuth] = {}
_TTL_SECONDS = 600


def put_pending(state: str, *, provider: str, email: str, name: str) -> None:
    _PENDING[state] = PendingOAuth(
        provider=provider,
        email=email.strip().lower(),
        name=name.strip(),
        created_at=time.time(),
    )


def pop_pending(state: str | None) -> PendingOAuth | None:
    if not state:
        return None
    _cleanup()
    return _PENDING.pop(state, None)


def _cleanup() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING.items() if now - v.created_at > _TTL_SECONDS]
    for k in expired:
        _PENDING.pop(k, None)
