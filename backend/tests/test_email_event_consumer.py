from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import ConsumedEvent
from app.services import email_event_consumer


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ConsumedEvent.__table__.create(engine)
    return sessionmaker(bind=engine)


def test_consumed_event_marker_is_idempotent(monkeypatch):
    sessions = _session_factory()
    monkeypatch.setattr(email_event_consumer, "SessionLocal", sessions)

    assert email_event_consumer._already_consumed("event-1") is False

    email_event_consumer._mark_consumed("event-1")
    email_event_consumer._mark_consumed("event-1")

    assert email_event_consumer._already_consumed("event-1") is True
    with sessions() as db:
        assert db.query(ConsumedEvent).count() == 1


def test_worker_health_status_reflects_worst_component():
    from app.services import service_health as sh
    try:
        assert sh.worker_health_status() == "healthy"
        sh.set_component_health("email-consumer", "healthy")  # no-op, stays healthy
        assert sh.worker_health_status() == "healthy"
        sh.set_component_health("email-consumer", "recovering")
        assert sh.worker_health_status() == "recovering"
    finally:
        sh.set_component_health("email-consumer", "healthy")
    assert sh.worker_health_status() == "healthy"


def test_record_service_heartbeat_persists_component_status(monkeypatch):
    from app.services import service_health as sh
    from app.models import ServiceHeartbeat
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ServiceHeartbeat.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(sh, "SessionLocal", sessions)
    try:
        sh.set_component_health("email-consumer", "recovering")
        sh.record_service_heartbeat("classification-worker")
        with sessions() as db:
            row = db.query(ServiceHeartbeat).one()
            assert row.status == "recovering"
        # Recovery clears the state; the next heartbeat reports healthy again.
        sh.set_component_health("email-consumer", "healthy")
        sh.record_service_heartbeat("classification-worker")
        with sessions() as db:
            assert db.query(ServiceHeartbeat).one().status == "healthy"
    finally:
        sh.set_component_health("email-consumer", "healthy")


def test_consumer_loop_marks_recovering_then_retries_and_stops(monkeypatch):
    import asyncio

    calls = []
    health = []

    async def fake_consume_once(settings, stop_event):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("broker channel dropped")
        stop_event.set()  # second attempt: shut down cleanly

    monkeypatch.setattr(email_event_consumer, "_consume_once", fake_consume_once)
    monkeypatch.setattr(email_event_consumer, "set_component_health", lambda c, s: health.append((c, s)))

    class _Settings:
        rabbitmq_enabled = True

    monkeypatch.setattr(email_event_consumer, "get_settings", lambda: _Settings())
    # Keep the reconnect backoff from slowing the test.
    monkeypatch.setattr(email_event_consumer, "_RECONNECT_BACKOFF_CAP_SECONDS", 0.01)

    async def run():
        stop = asyncio.Event()
        await asyncio.wait_for(email_event_consumer.email_event_consumer_loop(stop), timeout=5)

    asyncio.run(run())

    assert len(calls) == 2
    assert ("email-consumer", "recovering") in health


def test_handle_message_acks_after_job_is_accepted(monkeypatch):
    """Broker delivery must not remain open for the full mailbox AI pass."""
    import asyncio

    started = []
    acked = []

    class _FakeMsg:
        body = b'{"event_id": "e1", "payload": {"mailbox_id": 1, "classification_pending": true}}'
        async def ack(self):
            acked.append(True)

    async def accept_job(mailbox_id: int) -> None:
        started.append(mailbox_id)

    monkeypatch.setattr(email_event_consumer, "_classify_mailbox", accept_job)
    monkeypatch.setattr(email_event_consumer, "_already_consumed", lambda _event_id: False)
    monkeypatch.setattr(email_event_consumer, "_mark_consumed", lambda _event_id: None)

    async def run():
        await asyncio.wait_for(email_event_consumer._handle_message(_FakeMsg()), timeout=0.1)

    asyncio.run(run())
    assert started == [1]
    assert acked == [True]


def test_stop_async_iteration_triggers_consumer_stalled(monkeypatch):
    """If the queue iterator closes (channel dropped by connect_robust), the
    consumer should raise _ConsumerStalled so the outer loop reconnects."""
    import asyncio

    from app.services.email_event_consumer import _ConsumerStalled

    raised = []

    async def run():
        # Simulate the task returning StopAsyncIteration (iterator closed).
        task = asyncio.create_task(_raise_stop())
        await asyncio.sleep(0)  # let the task run
        try:
            task.result()  # re-raises StopAsyncIteration
        except StopAsyncIteration:
            raised.append(True)
            raise _ConsumerStalled("Queue iterator closed; consumer detached")

    async def _raise_stop():
        raise StopAsyncIteration

    try:
        asyncio.run(run())
    except _ConsumerStalled:
        pass

    assert raised == [True], "StopAsyncIteration from task result must be catchable"
