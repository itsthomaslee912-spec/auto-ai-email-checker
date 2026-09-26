from __future__ import annotations

import argparse
import os
from pathlib import Path

from sqlalchemy import MetaData, create_engine, func, inspect, select, text

from app.config import get_settings
from app.db import Base
from app import models  # noqa: F401


def migrate(sqlite_path: Path, postgres_url: str) -> None:
    if not sqlite_path.is_file():
        raise SystemExit(f"SQLite source not found: {sqlite_path}")
    if not postgres_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise SystemExit("Target DATABASE_URL must be PostgreSQL")

    source = create_engine(f"sqlite:///{sqlite_path.resolve().as_posix()}")
    target = create_engine(postgres_url)
    source_meta = MetaData()
    source_meta.reflect(bind=source)
    Base.metadata.create_all(bind=target)

    with target.begin() as conn:
        existing = sum(
            conn.execute(select(func.count()).select_from(table)).scalar_one()
            for table in Base.metadata.sorted_tables
            if inspect(target).has_table(table.name)
        )
        if existing:
            raise SystemExit("PostgreSQL target is not empty; migration aborted")

        with source.connect() as source_conn:
            for target_table in Base.metadata.sorted_tables:
                source_table = source_meta.tables.get(target_table.name)
                if source_table is None:
                    continue
                rows = source_conn.execute(select(source_table)).mappings().all()
                if not rows:
                    continue
                target_columns = set(target_table.c.keys())
                payload = [
                    {key: value for key, value in row.items() if key in target_columns}
                    for row in rows
                ]
                conn.execute(target_table.insert(), payload)
                print(f"Migrated {len(payload)} row(s) from {target_table.name}")

        preparer = target.dialect.identifier_preparer
        for table in Base.metadata.sorted_tables:
            primary_key = list(table.primary_key.columns)
            if len(primary_key) != 1 or not primary_key[0].autoincrement:
                continue
            column = primary_key[0]
            quoted_table = preparer.quote(table.name)
            quoted_column = preparer.quote(column.name)
            conn.execute(
                text(
                    "SELECT setval("
                    "pg_get_serial_sequence(:table_name, :column_name), "
                    f"COALESCE(MAX({quoted_column}), 1), "
                    f"MAX({quoted_column}) IS NOT NULL) FROM {quoted_table}"
                ),
                {"table_name": table.name, "column_name": column.name},
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Copy the local SQLite database into PostgreSQL")
    parser.add_argument("sqlite_path", type=Path)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "") or get_settings().database_url
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    migrate(args.sqlite_path, database_url)
