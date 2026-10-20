from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parents[2]  # repo root
_BACKEND = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    # Absolute paths only. A relative ".env"/"../.env" is resolved against each
    # process's current working directory, so the API and the native worker
    # processes (often started from different directories) could load a
    # different value for the same key — notably a duplicated
    # MICROSOFT_WEBHOOK_CLIENT_STATE — which desynchronized webhook subscription
    # creation from notification validation ("Invalid Outlook clientState").
    model_config = SettingsConfigDict(
        env_file=(
            str(_ROOT / ".env"),
            str(_BACKEND / ".env"),
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
    # Tune per replica so concurrent requests reuse warm PostgreSQL
    # connections without allowing a scaled deployment to exhaust the server.
    database_pool_size: int = 12
    database_max_overflow: int = 12
    database_pool_timeout_seconds: float = 5.0
    database_pool_recycle_seconds: int = 300
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

    # RabbitMQ is optional for embedded/local runs. When configured, durable
    # events are committed to the transactional outbox and relayed through a
    # topic exchange. An empty URL retains the database-backed fallback path.
    rabbitmq_url: str = ""
    rabbitmq_exchange: str = "aec.events"
    rabbitmq_routing_prefix: str = "aec"
    rabbitmq_prefetch_count: int = 8
    rabbitmq_outbox_batch_size: int = 100
    rabbitmq_outbox_poll_seconds: float = 0.25
    command_queue_poll_seconds: float = 0.25

    # Emit a warning for slow API requests while always returning a
    # Server-Timing header for browser and monitoring diagnostics.
    slow_request_threshold_ms: float = 750.0
    mailbox_job_lease_seconds: int = 600
    leader_lease_check_seconds: float = 10.0
    # Native worker processes initialize the schema on startup by default.
    run_db_init: bool = True

    websocket_heartbeat_seconds: int = 25
    # Route long-running commands to their owning worker and persist progress.
    # Tests and embedded legacy deployments can leave this disabled.
    distributed_runtime: bool = False
    # ``ai_only`` disables semantic regex heuristics and model-result guards.
    # ``hybrid`` preserves the existing desktop fallback during migration.
    email_classification_mode: str = "hybrid"

    @property
    def rabbitmq_enabled(self) -> bool:
        return bool(self.rabbitmq_url.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
