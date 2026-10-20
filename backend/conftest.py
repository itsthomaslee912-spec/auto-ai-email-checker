"""Test-session defaults.

The app reads required settings (notably ``DATABASE_URL``) from a repo ``.env``
file. Local development always has one, but CI and fresh checkouts do not, and
``app.db`` builds its engine at import time — so without a fallback, importing
any module that touches the database would fail during collection.

This supplies a throwaway on-disk SQLite database and dummy secrets, but only
when the developer has not already configured them. Environment variables take
precedence over the ``.env`` file in pydantic-settings, so we must not clobber a
real local configuration: we set a default only when both the environment and a
discoverable ``.env`` are absent.
"""

import os
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND.parent


def _has_configured_database() -> bool:
    if os.environ.get("DATABASE_URL"):
        return True
    return any((_REPO_ROOT / ".env").exists() or (_BACKEND / ".env").exists() for _ in (0,))


if not _has_configured_database():
    _db_path = Path(tempfile.gettempdir()) / "auto_ai_email_checker_test.db"
    _db_path.unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_db_path.as_posix()}"
    os.environ.setdefault("SESSION_SECRET", "test-session-secret")
    os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "")
