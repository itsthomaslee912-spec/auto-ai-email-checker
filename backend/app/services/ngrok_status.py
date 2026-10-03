from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from app.config import get_settings

_AGENT_URL = "http://127.0.0.1:4040/api/tunnels"
_CACHE_SECONDS = 30.0
_cached: tuple[float, dict] | None = None


def _public_host(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/").lower()


def _probe() -> dict:
    expected = (get_settings().webhook_base_url or "").strip().rstrip("/")
    try:
        response = httpx.get(_AGENT_URL, timeout=0.4)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {"state": "offline", "public_url": None, "expected_url": expected or None}

    tunnels = payload.get("tunnels") if isinstance(payload, dict) else None
    public_urls = []
    if isinstance(tunnels, list):
        for tunnel in tunnels:
            if not isinstance(tunnel, dict):
                continue
            url = str(tunnel.get("public_url") or "").strip().rstrip("/")
            if url.lower().startswith("https://"):
                public_urls.append(url)

    expected_host = _public_host(expected)
    matched = next((url for url in public_urls if _public_host(url) == expected_host), None) if expected_host else None
    if matched:
        state = "online"
        public_url = matched
    elif public_urls:
        state = "mismatch"
        public_url = public_urls[0]
    else:
        state = "offline"
        public_url = None
    return {"state": state, "public_url": public_url, "expected_url": expected or None}


def get_ngrok_status() -> dict:
    """Report whether the local ngrok agent is serving the configured webhook URL."""
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < _CACHE_SECONDS:
        return _cached[1]
    status = _probe()
    _cached = (now, status)
    return status


def clear_ngrok_status_cache() -> None:
    global _cached
    _cached = None
