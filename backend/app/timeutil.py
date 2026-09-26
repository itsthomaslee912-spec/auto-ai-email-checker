from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def as_utc(value: datetime | None) -> datetime | None:
    """Return an aware UTC datetime. Naive values are assumed to already be UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UtcDateTime(TypeDecorator):
    """Store datetimes as UTC.

    SQLite's DateTime dialect drops tzinfo and keeps wall-clock values, so a
    timestamp like 09:15-04:00 would be persisted as 09:15 and later read as
    UTC — shifting displayed local time. Always convert to UTC on write.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            return value
        utc = as_utc(value)
        assert utc is not None
        # Persist naive UTC on SQLite so the driver cannot re-interpret offsets.
        if dialect.name == "sqlite":
            return utc.replace(tzinfo=None)
        return utc

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            return value
        return as_utc(value)
