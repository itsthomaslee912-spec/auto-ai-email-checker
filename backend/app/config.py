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
    # A healthy, verified webhook delivers mail immediately; re-running its
    # full incremental sync on every 120s tick only guards against an
    # exhausted retry window or missed delivery, so it does not need the
    # same cadence as the active polling path.
    auto_sync_webhook_safety_net_seconds: int = 900

    # Runtime separation. ``all`` preserves the desktop/development behavior;
    # container deployments run ``api`` and ``worker`` as separate processes.
    runtime_role: str = "all"

    # Kafka is optional during the migration. When configured, every durable
    # activity event is written to the transactional outbox and published by
    # the worker process. Empty bootstrap servers keep the existing local
    # single-process delivery path working.
    kafka_bootstrap_servers: str = ""
    kafka_client_id: str = "auto-ai-email-checker"
    kafka_topic_prefix: str = "aec"
    kafka_security_protocol: str = "PLAINTEXT"
    kafka_sasl_mechanism: str = ""
    kafka_sasl_username: str = ""
    kafka_sasl_password: str = ""
    kafka_outbox_batch_size: int = 100
    kafka_outbox_poll_seconds: float = 1.0

    websocket_heartbeat_seconds: int = 25
    # ``ai_only`` disables semantic regex heuristics and model-result guards.
    # ``hybrid`` preserves the existing desktop fallback during migration.
    email_classification_mode: str = "hybrid"

    @property
    def kafka_enabled(self) -> bool:
        return bool(self.kafka_bootstrap_servers.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
