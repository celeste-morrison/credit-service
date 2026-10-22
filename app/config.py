"""Runtime configuration loaded from ``CREDITS_*`` environment variables."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CLIENT = "default"
"""Client name for API keys configured without a ``client:`` prefix."""


class Settings(BaseSettings):
    """Service settings, read from the environment or a local ``.env`` file.

    Attributes:
        mongo_url: MongoDB connection string. Must target a replica set, which
            multi-document transactions require.
        mongo_db: Database name.
        mongo_timeout_ms: Upper bound for any single MongoDB operation or
            transaction, so outages fail fast instead of hanging requests.
        redis_url: Celery broker URL.
        task_max_retries: Worker retries for transient MongoDB failures.
        task_retry_backoff_max_s: Cap on the exponential retry delay.
        reconcile_after_s: Age after which a still-pending credit is re-enqueued
            by the reconciliation job.
        reconcile_interval_s: How often the reconciliation job runs.
        idempotency_key_ttl_s: How long an Idempotency-Key is honoured. Older
            keys are released by a periodic job so the index stays bounded.
        max_body_bytes: Largest request body the API will read.
        rate_limit_per_minute: Requests each API client may make per minute.
        docs_enabled: Serve ``/docs``, ``/redoc`` and ``/openapi.json``. Off by
            default so production does not publish the API schema.
        log_level: Log level for the JSON logs.
        api_keys: Comma-separated ``client:key`` pairs, for example
            ``billing:k1,payouts:k2``. The client name is recorded on every
            credit and scopes idempotency keys and rate limits. A bare ``key``
            belongs to the client ``default``. Several keys per client allow
            rotation without downtime. Empty rejects every authenticated request.
    """

    model_config = SettingsConfigDict(env_prefix="CREDITS_", env_file=".env", extra="ignore")

    mongo_url: str = "mongodb://localhost:27017/?directConnection=true"
    mongo_db: str = "credits"
    mongo_timeout_ms: int = 5000
    redis_url: str = "redis://localhost:6379/0"
    task_max_retries: int = 10
    task_retry_backoff_max_s: int = 60
    reconcile_after_s: int = 120
    reconcile_interval_s: int = 60
    idempotency_key_ttl_s: int = 24 * 60 * 60
    max_body_bytes: int = 16 * 1024
    rate_limit_per_minute: int = 600
    docs_enabled: bool = False
    log_level: str = "INFO"
    api_keys: SecretStr = SecretStr("")

    @property
    def mongo_timeout_s(self) -> float:
        """The MongoDB operation timeout in seconds."""
        return self.mongo_timeout_ms / 1000

    def api_clients(self) -> dict[str, str]:
        """Parse ``api_keys`` into a mapping of key to client name.

        Returns:
            Each configured key mapped to the client it identifies.
        """
        clients: dict[str, str] = {}
        for entry in self.api_keys.get_secret_value().split(","):
            client, _, key = entry.strip().rpartition(":")
            if key:
                clients[key] = client or DEFAULT_CLIENT
        return clients


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Returns:
        Settings loaded once and cached for the life of the process.
    """
    return Settings()
