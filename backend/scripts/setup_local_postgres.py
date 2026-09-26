from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def set_database_url(path: Path, database_url: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"DATABASE_URL={database_url}"
    output: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("DATABASE_URL="):
            if not replaced:
                output.append(replacement)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        if output and output[-1]:
            output.append("")
        output.append(replacement)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def main() -> None:
    current_super_password = required("CURRENT_PG_SUPER_PASSWORD")
    new_super_password = required("NEW_PG_SUPER_PASSWORD")
    app_password = required("PG_APP_PASSWORD")
    root = Path(required("PROJECT_ROOT"))
    host = "127.0.0.1"
    port = 5432
    role = "email_checker"
    database = "email_checker"

    with psycopg.connect(
        host=host,
        port=port,
        dbname="postgres",
        user="postgres",
        password=current_super_password,
        autocommit=True,
    ) as conn:
        role_exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if role_exists:
            conn.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(app_password)
                )
            )
        else:
            conn.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(app_password)
                )
            )

        database_exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if not database_exists:
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
        else:
            conn.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )

        conn.execute(
            sql.SQL("ALTER ROLE postgres WITH PASSWORD {}").format(
                sql.Literal(new_super_password)
            )
        )

    with psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=role,
        password=app_password,
    ) as conn:
        value = conn.execute("SELECT current_database(), current_user").fetchone()
        if value != (database, role):
            raise SystemExit("Application database verification failed")

    database_url = (
        f"postgresql+psycopg://{role}:{quote(app_password, safe='')}@{host}:{port}/{database}"
    )
    set_database_url(root / ".env", database_url)
    set_database_url(root / "backend" / ".env", database_url)

    admin_password_file = root / ".local" / "postgresql" / "admin-password.txt"
    admin_password_file.write_text(new_super_password + "\n", encoding="utf-8")
    print("Local PostgreSQL role, database, credentials, and environment files are configured")


if __name__ == "__main__":
    main()
