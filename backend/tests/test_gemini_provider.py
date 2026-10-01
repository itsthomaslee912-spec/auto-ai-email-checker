from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import ai_settings
from app.classify.openai_classifier import classify_email
from app.db import Base, get_db
from app.models import AiSettings
from app.services.ai_backend import AiBackend, GEMINI_OPENAI_BASE_URL


def test_gemini_backend_uses_google_openai_compatible_endpoint():
    client = AiBackend(
        provider="gemini",
        model="gemini-test",
        api_key="gemini-key",
    ).client()

    assert str(client.base_url) == GEMINI_OPENAI_BASE_URL


def test_selected_gemini_backend_is_used_for_classification(monkeypatch):
    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                id="gemini-response",
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "label": "other",
                    "confidence": 0.91,
                })))],
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    backend = SimpleNamespace(
        provider="gemini",
        model="gemini-test",
        api_key="gemini-key",
        client=lambda: fake_client,
    )
    monkeypatch.setattr("app.classify.openai_classifier.has_saved_ai_settings", lambda: True)
    monkeypatch.setattr("app.classify.openai_classifier.get_ai_backend", lambda: backend)
    monkeypatch.setattr("app.classify.openai_classifier.get_active_system_prompt", lambda: "Classify this email.")
    monkeypatch.setattr(
        "app.classify.openai_classifier.AsyncOpenAI",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("OpenAI client should not be used")),
    )

    result = asyncio.run(classify_email(
        subject="Hello",
        sender="person@example.com",
        body_text="A generic message.",
        snippet="A generic message.",
    ))

    assert captured["model"] == "gemini-test"
    assert result.label == "other"
    assert result.confidence == 0.91


def test_gemini_settings_are_saved_without_exposing_the_key():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    app = FastAPI()
    app.include_router(ai_settings.router)

    def override_get_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    response = client.put("/api/ai-settings", json={
        "provider": "gemini",
        "openai_model": "gpt-4o-mini",
        "gemini_model": "gemini-test",
        "gemini_api_key": "secret-gemini-key",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "qwen2.5:14b-instruct",
    })

    assert response.status_code == 200
    assert response.json()["provider"] == "gemini"
    assert response.json()["gemini_key_configured"] is True
    assert "secret-gemini-key" not in response.text
    assert client.get("/api/ai-settings/status").json() == {
        "provider": "gemini",
        "model": "gemini-test",
        "ready": True,
    }
    with session_factory() as db:
        row = db.get(AiSettings, 1)
        assert row is not None
        assert row.gemini_key_enc != "secret-gemini-key"
