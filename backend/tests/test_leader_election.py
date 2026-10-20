from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.services import leader_election


def test_lock_key_is_stable_signed_64_bit_value():
    first = leader_election._lock_key("sync-schedulers")
    assert first == leader_election._lock_key("sync-schedulers")
    assert -(1 << 63) <= first < (1 << 63)


def test_non_postgres_leader_runs_and_stops(monkeypatch):
    sqlite_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(leader_election, "engine", sqlite_engine)

    async def exercise():
        stop_event = asyncio.Event()
        started = asyncio.Event()

        async def singleton_loop(worker_stop: asyncio.Event):
            started.set()
            await worker_stop.wait()

        task = asyncio.create_task(
            leader_election.leader_elected_loops(
                stop_event, "test-election", (singleton_loop,)
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise())
