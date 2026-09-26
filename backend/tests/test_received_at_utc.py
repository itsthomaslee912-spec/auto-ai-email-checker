from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import Column, Integer, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.email.gmail import normalize_gmail_message
from app.timeutil import UtcDateTime, as_utc


class Base(DeclarativeBase):
    pass


class Sample(Base):
    __tablename__ = "sample_times"
    id = Column(Integer, primary_key=True)
    received_at = Column(UtcDateTime())


def test_as_utc_converts_offset():
    edt = parsedate_to_datetime("Mon, 21 Sep 2026 09:15:05 -0400")
    utc = as_utc(edt)
    assert utc == datetime(2026, 9, 21, 13, 15, 5, tzinfo=timezone.utc)


def test_sqlite_stores_utc_wall_clock_not_local():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    edt = datetime(2026, 9, 21, 9, 15, 5, tzinfo=timezone(timedelta(hours=-4)))
    db.add(Sample(received_at=edt))
    db.commit()

    raw = db.execute(text("SELECT received_at FROM sample_times")).scalar()
    assert raw.startswith("2026-09-21 13:15:05")

    back = db.query(Sample).one().received_at
    assert back == datetime(2026, 9, 21, 13, 15, 5, tzinfo=timezone.utc)
    db.close()


def test_gmail_falls_back_to_internal_date_utc():
    edt = parsedate_to_datetime("Mon, 21 Sep 2026 09:15:05 -0400")
    ms = int(edt.timestamp() * 1000)
    raw = {
        "id": "msg1",
        "threadId": "t1",
        "snippet": "hi",
        "internalDate": str(ms),
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "From", "value": "a@b.com"},
                {"name": "Date", "value": "Mon, 21 Sep 2026 09:15:05 -0400"},
            ],
            "body": {"data": ""},
        },
        "labelIds": ["INBOX"],
    }
    norm = normalize_gmail_message(raw)
    assert norm["received_at"] == datetime(2026, 9, 21, 13, 15, 5, tzinfo=timezone.utc)


def test_gmail_prefers_received_header_over_stale_date():
    # Waymo-style: Date/internalDate hours before actual Gmail delivery.
    raw = {
        "id": "msg-waymo",
        "threadId": "t1",
        "snippet": "jobs",
        "internalDate": str(int(datetime(2026, 9, 21, 16, 34, 16, tzinfo=timezone.utc).timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Here are some new roles at Waymo"},
                {"name": "From", "value": "waymo@example.com"},
                {
                    "name": "Received",
                    "value": (
                        "by mx.google.com with UTF8SMTPS id abc; "
                        "Mon, 21 Sep 2026 14:21:13 -0700 (PDT)"
                    ),
                },
                {"name": "Date", "value": "Mon, 21 Sep 2026 16:34:16 +0000"},
            ],
            "body": {"data": ""},
        },
        "labelIds": ["INBOX"],
    }
    norm = normalize_gmail_message(raw)
    assert norm["received_at"] == datetime(2026, 9, 21, 21, 21, 13, tzinfo=timezone.utc)
