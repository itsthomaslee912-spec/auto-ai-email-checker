"""Resolve OAuth callback and post-login frontend URLs for local + remote clients."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse

from app.config import Settings, get_settings


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return True
    lowered = host.strip().lower().rstrip(".")
    if lowered in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return bool(ip_address(lowered).is_loopback)
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    try:
        return _is_loopback_host(urlparse(url).hostname)
    except ValueError:
        return True


def public_backend_base(settings: Settings | None = None) -> str:
    """Prefer a non-loopback backend URL so OAuth callbacks work off this machine."""
    cfg = settings or get_settings()
    for candidate in (cfg.backend_public_url, cfg.webhook_base_url):
        base = (candidate or "").strip().rstrip("/")
        if base and not is_loopback_url(base):
            return base
    return (cfg.backend_public_url or cfg.webhook_base_url or "http://localhost:8000").rstrip("/")


def google_oauth_redirect_uri(settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    explicit = (cfg.google_redirect_uri or "").strip()
    if explicit and not is_loopback_url(explicit):
        return explicit
    base = public_backend_base(cfg)
    if not is_loopback_url(base):
        return f"{base}/api/auth/google/callback"
    return explicit or "http://localhost:8000/api/auth/google/callback"


def microsoft_oauth_redirect_uri(settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    explicit = (cfg.microsoft_redirect_uri or "").strip()
    if explicit and not is_loopback_url(explicit):
        return explicit
    base = public_backend_base(cfg)
    if not is_loopback_url(base):
        return f"{base}/api/auth/microsoft/callback"
    return explicit or "http://localhost:8000/api/auth/microsoft/callback"


def _is_private_host(host: str | None) -> bool:
    if not host:
        return False
    lowered = host.strip().lower().rstrip(".")
    try:
        addr = ip_address(lowered)
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local)


def _is_trusted_frontend_host(host: str | None, cfg: Settings) -> bool:
    if _is_loopback_host(host) or _is_private_host(host):
        return True
    configured_host = urlparse(cfg.frontend_origin or "").hostname
    return bool(host) and bool(configured_host) and host.strip().lower().rstrip(".") == configured_host.lower()


def sanitize_frontend_return(return_to: str | None, settings: Settings | None = None) -> str:
    """Allow the browser origin the user actually opened, as long as it's the configured
    frontend or a loopback/LAN address; anything else (e.g. an attacker-supplied public
    host) falls back to FRONTEND_ORIGIN to avoid an open redirect."""
    cfg = settings or get_settings()
    fallback = (cfg.frontend_origin or "http://localhost:5173").rstrip("/")
    raw = (return_to or "").strip()
    if not raw:
        return fallback
    try:
        parsed = urlparse(raw)
    except ValueError:
        return fallback
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return fallback
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return fallback
    if not _is_trusted_frontend_host(parsed.hostname, cfg):
        return fallback
    return f"{parsed.scheme}://{parsed.netloc}"
