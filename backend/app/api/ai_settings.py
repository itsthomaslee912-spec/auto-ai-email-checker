from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt_token, encrypt_token
from app.db import get_db
from app.models import AiSettings

router = APIRouter(prefix="/api/ai-settings", tags=["ai-settings"])


class AiSettingsIn(BaseModel):
    provider: str
    openai_model: str = Field(min_length=1, max_length=128)
    openai_api_key: str | None = None
    openai_admin_key: str | None = None
    ollama_url: str = Field(min_length=1, max_length=255)
    ollama_model: str = Field(min_length=1, max_length=128)


def _view(row: AiSettings | None) -> dict:
    defaults = get_settings()
    return {
        "provider": row.provider if row else "openai",
        "openai_model": row.openai_model if row else defaults.openai_model,
        "openai_key_configured": bool((row.openai_key_enc if row else "") or defaults.openai_api_key),
        "openai_admin_key_configured": bool(row and row.openai_admin_key_enc),
        "ollama_url": row.ollama_url if row else "http://192.168.2.230:11440",
        "ollama_model": row.ollama_model if row else "qwen2.5:14b-instruct",
        "billing_url": "https://platform.openai.com/settings/organization/billing/overview",
    }


@router.get("")
def read_ai_settings(db: Session = Depends(get_db)) -> dict:
    return _view(db.get(AiSettings, 1))


@router.put("")
def save_ai_settings(payload: AiSettingsIn, db: Session = Depends(get_db)) -> dict:
    if payload.provider not in {"openai", "ollama"}:
        raise HTTPException(422, "Choose OpenAI or Ollama")
    parsed = urlparse(payload.ollama_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, "Enter a valid Ollama server URL")
    row = db.get(AiSettings, 1)
    if row is None:
        row = AiSettings(id=1)
        db.add(row)
    row.provider = payload.provider
    row.openai_model = payload.openai_model.strip()
    row.ollama_url = payload.ollama_url.rstrip("/")
    row.ollama_model = payload.ollama_model.strip()
    if payload.openai_api_key:
        row.openai_key_enc = encrypt_token(payload.openai_api_key.strip())
    if payload.openai_admin_key:
        row.openai_admin_key_enc = encrypt_token(payload.openai_admin_key.strip())
    db.commit()
    return _view(row)


@router.get("/openai-costs")
async def read_openai_costs(db: Session = Depends(get_db)) -> dict:
    row = db.get(AiSettings, 1)
    if row is None or not row.openai_admin_key_enc:
        raise HTTPException(400, "OpenAI organization admin key is not configured")
    now = datetime.now(timezone.utc)
    start = int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())
    try:
        key = decrypt_token(row.openai_admin_key_enc)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                "https://api.openai.com/v1/organization/costs",
                headers={"Authorization": f"Bearer {key}"},
                params={"start_time": start, "limit": 31},
            )
            if response.status_code in {401, 403}:
                raise HTTPException(400, "OpenAI rejected the organization admin key")
            response.raise_for_status()
            buckets = response.json().get("data", [])
            spent = sum(
                float(result.get("amount", {}).get("value") or 0)
                for bucket in buckets for result in bucket.get("results", [])
            )
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(502, "Could not load OpenAI organization costs") from exc
    return {
        "month_spend_usd": round(spent, 6),
        "currency": "usd",
        "period_start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "as_of": now.isoformat(),
        "credit_balance_available": False,
    }


@router.get("/ollama-models")
async def list_ollama_models(url: str = "http://192.168.2.230:11440") -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, "Enter a valid Ollama server URL")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f"{url.rstrip('/')}/api/tags")
            response.raise_for_status()
            names = [str(item["name"]) for item in response.json().get("models", []) if item.get("name")]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(502, "Cannot reach Ollama or read its model list") from exc
    return {"models": names}
