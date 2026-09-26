from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    settings = get_settings()
    raw = settings.token_encryption_key.strip()
    if not raw:
        # Deterministic local fallback so the app boots without config.
        # Replace with a real Fernet key in production.
        raw = urlsafe_b64encode(sha256(b"dev-email-checker-key").digest()).decode()
    elif len(raw) != 44:
        raw = urlsafe_b64encode(sha256(raw.encode()).digest()).decode()
    return Fernet(raw.encode() if isinstance(raw, str) else raw)


def encrypt_token(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_token(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt token; check TOKEN_ENCRYPTION_KEY") from exc
