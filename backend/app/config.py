from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parents[2]  # repo root
_BACKEND = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(
            str(_ROOT / ".env"),
            str(_BACKEND / ".env"),
            ".env",
            "../.env",
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/auth/google/callback"
    gmail_pubsub_topic: str = ""
    gmail_pubsub_audience: str = ""
    gmail_pubsub_service_account: str = ""

    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant: str = "common"
    microsoft_redirect_uri: str = "http://localhost:8000/api/auth/microsoft/callback"
    microsoft_webhook_client_state: str = "change-me-to-a-long-random-string"

    database_url: str
    token_encryption_key: str = ""
    webhook_base_url: str = "http://localhost:8000"
    frontend_origin: str = "http://localhost:5173"
    backend_public_url: str = "http://localhost:8000"
    session_secret: str = "change-me-session-secret"
    # Max messages to pull on initial import / Full rescan (paged). Not a Gmail API hard limit.
    mail_sync_max: int = 10000
    # JSON map of mailbox addresses to sender addresses that should never be imported.
    mail_sync_excluded_senders: str = "{}"
    auto_sync_interval_seconds: int = 120
    auto_sync_max_messages: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()
