from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.crypto import decrypt_token
from app.db import SessionLocal
from app.models import AiSettings


@dataclass(frozen=True)
class AiBackend:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None

    def client(self) -> AsyncOpenAI:
        if self.provider == "ollama":
            return AsyncOpenAI(
                api_key="ollama",
                base_url=f"{self.base_url.rstrip('/')}/v1",
                timeout=180.0,
            )
        return AsyncOpenAI(api_key=self.api_key)


def get_ai_backend() -> AiBackend:
    defaults = get_settings()
    with SessionLocal() as db:
        saved = db.get(AiSettings, 1)
        if saved and saved.provider == "ollama":
            return AiBackend(
                provider="ollama", model=saved.ollama_model,
                api_key="ollama", base_url=saved.ollama_url,
            )
        key = decrypt_token(saved.openai_key_enc) if saved and saved.openai_key_enc else defaults.openai_api_key
        return AiBackend(
            provider="openai",
            model=saved.openai_model if saved else defaults.openai_model,
            api_key=key,
        )


def has_saved_ai_settings() -> bool:
    try:
        with SessionLocal() as db:
            return db.get(AiSettings, 1) is not None
    except OperationalError:
        return False
