from types import SimpleNamespace

import httpx

from app.services.ngrok_status import clear_ngrok_status_cache, get_ngrok_status


def _settings(url: str):
    return SimpleNamespace(webhook_base_url=url)


def test_ngrok_status_is_online_when_the_tunnel_matches(monkeypatch):
    clear_ngrok_status_cache()
    monkeypatch.setattr("app.services.ngrok_status.get_settings", lambda: _settings("https://example.ngrok-free.dev/"))

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "tunnels": [
                    {"public_url": "http://example.ngrok-free.dev"},
                    {"public_url": "https://example.ngrok-free.dev"},
                ]
            }

    monkeypatch.setattr("app.services.ngrok_status.httpx.get", lambda *_args, **_kwargs: Response())

    status = get_ngrok_status()
    assert status["state"] == "online"
    assert status["public_url"] == "https://example.ngrok-free.dev"


def test_ngrok_status_is_a_mismatch_when_the_public_address_differs(monkeypatch):
    clear_ngrok_status_cache()
    monkeypatch.setattr("app.services.ngrok_status.get_settings", lambda: _settings("https://example.ngrok-free.dev"))

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"tunnels": [{"public_url": "https://other.ngrok-free.dev"}]}

    monkeypatch.setattr("app.services.ngrok_status.httpx.get", lambda *_args, **_kwargs: Response())

    status = get_ngrok_status()
    assert status["state"] == "mismatch"
    assert status["public_url"] == "https://other.ngrok-free.dev"


def test_ngrok_status_is_offline_when_the_agent_is_down(monkeypatch):
    clear_ngrok_status_cache()
    monkeypatch.setattr("app.services.ngrok_status.get_settings", lambda: _settings("https://example.ngrok-free.dev"))

    def fail(*_args, **_kwargs):
        raise httpx.ConnectError("ngrok agent is not running")

    monkeypatch.setattr("app.services.ngrok_status.httpx.get", fail)

    status = get_ngrok_status()
    assert status == {
        "state": "offline",
        "public_url": None,
        "expected_url": "https://example.ngrok-free.dev",
    }
