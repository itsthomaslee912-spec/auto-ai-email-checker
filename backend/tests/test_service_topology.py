from __future__ import annotations

from app.workers import classification, event_relay, notification, sync


def test_background_services_have_single_responsibilities(monkeypatch):
    captured: dict[str, tuple] = {}

    async def fake_run_service(name, loops, **_kwargs):
        captured[name] = tuple(loops)

    monkeypatch.setattr(sync, "run_service", fake_run_service)
    monkeypatch.setattr(classification, "run_service", fake_run_service)
    monkeypatch.setattr(event_relay, "run_service", fake_run_service)
    monkeypatch.setattr(notification, "run_service", fake_run_service)

    import asyncio

    asyncio.run(sync.run())
    asyncio.run(classification.run())
    asyncio.run(event_relay.run())
    asyncio.run(notification.run())

    assert {loop.__name__ for loop in captured["sync-worker"]} == {
        "webhook_queue_loop",
        "sync_command_loop",
        "_scheduler_leader_loop",
    }
    assert {loop.__name__ for loop in captured["classification-worker"]} == {
        "email_event_consumer_loop",
        "classification_command_loop",
        "_maintenance_leader_loop",
    }
    assert [loop.__name__ for loop in captured["event-relay"]] == [
        "outbox_publisher_loop"
    ]
    assert [loop.__name__ for loop in captured["notification-service"]] == [
        "notification_consumer_loop"
    ]
