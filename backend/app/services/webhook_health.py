from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock

_lock = Lock()
_delivery: dict[str, dict[str, datetime | str | None]] = {}


def record_webhook_delivery_success(provider: str) -> None:
    with _lock:
        state = _delivery.setdefault(provider, {})
        state["last_success_at"] = datetime.now(timezone.utc)
        state["last_error"] = None


def record_webhook_delivery_failure(provider: str, message: str) -> None:
    with _lock:
        state = _delivery.setdefault(provider, {})
        state["last_failure_at"] = datetime.now(timezone.utc)
        state["last_error"] = message


def webhook_delivery_is_healthy(provider: str) -> bool:
    with _lock:
        state = dict(_delivery.get(provider) or {})
    success = state.get("last_success_at")
    failure = state.get("last_failure_at")
    return isinstance(success, datetime) and (
        not isinstance(failure, datetime) or success >= failure
    )


def webhook_delivery_status(provider: str) -> dict[str, str | bool | None]:
    with _lock:
        state = dict(_delivery.get(provider) or {})
    success = state.get("last_success_at")
    failure = state.get("last_failure_at")
    return {
        "healthy": webhook_delivery_is_healthy(provider),
        "last_success_at": success.isoformat() if isinstance(success, datetime) else None,
        "last_failure_at": failure.isoformat() if isinstance(failure, datetime) else None,
        "last_error": str(state.get("last_error")) if state.get("last_error") else None,
    }


def reset_webhook_delivery_health() -> None:
    """Clear process-local delivery state. Intended for startup and tests."""
    with _lock:
        _delivery.clear()
