"""Desktop entry point for the packaged FastAPI sidecar.

The desktop application owns an isolated PostgreSQL cluster in the current
Windows user's application-data directory. Installed program files remain
read-only, and upgrades do not remove mailbox data.
"""
from __future__ import annotations

import atexit
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql


APP_NAME = "Auto AI Email Checker"
POSTGRES_PORT = int(os.environ.get("AUTO_EMAIL_POSTGRES_PORT", "55432"))
POSTGRES_USER = "email_checker"
POSTGRES_DATABASE = "email_checker"
_postgres_started_here = False


def _user_data_dir() -> Path:
    configured = os.environ.get("AUTO_EMAIL_DATA_DIR", "").strip()
    root = Path(configured) if configured else Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _postgres_root() -> Path:
    configured = os.environ.get("AUTO_EMAIL_POSTGRES_ROOT", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[1] / ".local" / "postgresql",
    ]
    for candidate in candidates:
        if candidate and (candidate / "bin" / "pg_ctl.exe").is_file():
            return candidate
    raise RuntimeError("Bundled PostgreSQL runtime was not found")


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    hide_window: bool = True,
) -> subprocess.CompletedProcess[str]:
    output_options = (
        {"capture_output": True}
        if hide_window
        else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    )
    return subprocess.run(
        command,
        env=env,
        check=False,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if hide_window else 0,
        **output_options,
    )


def _ensure_postgres(data_dir: Path, postgres_root: Path) -> str:
    global _postgres_started_here
    bin_dir = postgres_root / "bin"
    cluster_dir = data_dir / "postgres-data"
    log_dir = data_dir / "logs"
    password_file = data_dir / "postgres-password.txt"
    log_dir.mkdir(parents=True, exist_ok=True)

    if not (cluster_dir / "PG_VERSION").exists():
        password = secrets.token_urlsafe(32)
        password_file.write_text(password, encoding="utf-8")
        init_password_file = data_dir / ".initdb-password"
        try:
            init_password_file.write_text(password, encoding="utf-8")
            result = _run([
                str(bin_dir / "initdb.exe"),
                "-D", str(cluster_dir),
                "-U", POSTGRES_USER,
                "--pwfile", str(init_password_file),
                "--auth-host=scram-sha-256",
                "--auth-local=trust",
                "--encoding=UTF8",
                "--no-locale",
            ])
            if result.returncode != 0:
                raise RuntimeError(f"Could not initialize local PostgreSQL: {result.stderr.strip()}")
        finally:
            init_password_file.unlink(missing_ok=True)

    if not password_file.exists():
        raise RuntimeError(
            f"PostgreSQL password file is missing: {password_file}. "
            "Restore it or remove postgres-data to create a new empty database."
        )
    password = password_file.read_text(encoding="utf-8").strip()

    pg_ctl = str(bin_dir / "pg_ctl.exe")
    status = _run([pg_ctl, "-D", str(cluster_dir), "status"])
    if status.returncode != 0:
        result = _run([
            pg_ctl,
            "-D", str(cluster_dir),
            "-l", str(log_dir / "postgresql.log"),
            "-o", f"-h 127.0.0.1 -p {POSTGRES_PORT}",
            "-w", "start",
        ], hide_window=False)
        if result.returncode != 0:
            raise RuntimeError(
                "Could not start local PostgreSQL. "
                f"See {log_dir / 'postgresql.log'} for details."
            )
        _postgres_started_here = True

    with psycopg.connect(
        host="127.0.0.1",
        port=POSTGRES_PORT,
        dbname="postgres",
        user=POSTGRES_USER,
        password=password,
        autocommit=True,
    ) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (POSTGRES_DATABASE,),
        ).fetchone()
        if not exists:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(POSTGRES_DATABASE),
                    sql.Identifier(POSTGRES_USER),
                )
            )
    return password


def _stop_postgres() -> None:
    if not _postgres_started_here:
        return
    try:
        data_dir = _user_data_dir() / "postgres-data"
        pg_ctl = _postgres_root() / "bin" / "pg_ctl.exe"
        _run(
            [str(pg_ctl), "-D", str(data_dir), "stop", "-m", "fast"],
            hide_window=False,
        )
    except Exception:
        pass


def main() -> None:
    data_dir = _user_data_dir()
    postgres_root = _postgres_root()
    password = _ensure_postgres(data_dir, postgres_root)
    atexit.register(_stop_postgres)

    encoded_password = quote(password, safe="")
    os.environ["DATABASE_URL"] = (
        f"postgresql+psycopg://{POSTGRES_USER}:{encoded_password}"
        f"@127.0.0.1:{POSTGRES_PORT}/{POSTGRES_DATABASE}"
    )
    os.environ.setdefault("FRONTEND_ORIGIN", "tauri://localhost")
    os.environ.setdefault("BACKEND_PUBLIC_URL", "http://127.0.0.1:8000")
    os.environ["PATH"] = str(postgres_root / "bin") + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("DISABLE_SQLALCHEMY_CEXT_RUNTIME", "1")
    os.environ.setdefault("PSYCOPG_IMPL", "python")

    # Pydantic Settings reads an optional per-user .env after this directory change.
    os.chdir(data_dir)

    import uvicorn
    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
