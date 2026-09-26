"""Desktop entry point for the packaged FastAPI sidecar.

The Tauri shell starts this executable on localhost.  Keep user data outside
the installed application directory so upgrades do not delete mail data.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _user_data_dir() -> Path:
    root = Path(os.environ.get("APPDATA", Path.home())) / "Auto AI Email Checker"
    root.mkdir(parents=True, exist_ok=True)
    return root


def main() -> None:
    data_dir = _user_data_dir()
    # Environment variables take precedence over .env values in Settings.
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{(data_dir / 'email_checker.db').as_posix()}")
    os.environ.setdefault("FRONTEND_ORIGIN", "tauri://localhost")
    os.environ.setdefault("BACKEND_PUBLIC_URL", "http://127.0.0.1:8000")

    # Let a desktop user configure OAuth and OpenAI without putting secrets in
    # the installed executable.  Pydantic Settings also reads environment vars.
    os.chdir(data_dir)

    import uvicorn
    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
