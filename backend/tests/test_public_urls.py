from types import SimpleNamespace

from app.auth.public_urls import (
    google_oauth_redirect_uri,
    microsoft_oauth_redirect_uri,
    public_backend_base,
    sanitize_frontend_return,
)


def _settings(**overrides):
    base = dict(
        backend_public_url="http://localhost:8000",
        webhook_base_url="http://localhost:8000",
        google_redirect_uri="http://localhost:8000/api/auth/google/callback",
        microsoft_redirect_uri="http://localhost:8000/api/auth/microsoft/callback",
        frontend_origin="http://localhost:5173",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_public_backend_prefers_non_loopback_webhook():
    settings = _settings(webhook_base_url="https://abc.ngrok-free.dev")
    assert public_backend_base(settings) == "https://abc.ngrok-free.dev"


def test_google_redirect_uses_public_backend_when_local_default():
    settings = _settings(webhook_base_url="https://abc.ngrok-free.dev")
    assert google_oauth_redirect_uri(settings) == "https://abc.ngrok-free.dev/api/auth/google/callback"
    assert microsoft_oauth_redirect_uri(settings) == "https://abc.ngrok-free.dev/api/auth/microsoft/callback"


def test_explicit_public_redirect_is_kept():
    settings = _settings(
        google_redirect_uri="https://custom.example/api/auth/google/callback",
        webhook_base_url="https://abc.ngrok-free.dev",
    )
    assert google_oauth_redirect_uri(settings) == "https://custom.example/api/auth/google/callback"


def test_sanitize_frontend_return_accepts_lan_origin():
    settings = _settings()
    assert sanitize_frontend_return("http://192.168.2.156:5173", settings) == "http://192.168.2.156:5173"
    assert sanitize_frontend_return("http://evil.example/phish", settings) == "http://localhost:5173"
    assert sanitize_frontend_return(None, settings) == "http://localhost:5173"
