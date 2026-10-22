"""Runtime configuration loaded from ``CREDITS_*`` environment variables."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        max_body_bytes: Largest request body the API will read.
        log_level: Log level for the JSON logs.
        api_keys: Comma-separated service API keys. Several keys allow rotation
            without downtime. Empty rejects every authenticated request.
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
    max_body_bytes: int = 16 * 1024
    log_level: str = "INFO"
    api_keys: SecretStr = SecretStr("")

    @property
    def mongo_timeout_s(self) -> float:
        """The MongoDB operation timeout in seconds."""
        return self.mongo_timeout_ms / 1000

    def api_key_set(self) -> frozenset[str]:
        """Parse ``api_keys`` into a set of non-empty keys.

        Returns:
            The configured keys, stripped of surrounding whitespace.
        """
        raw = self.api_keys.get_secret_value()
        return frozenset(key.strip() for key in raw.split(",") if key.strip())


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Returns:
        Settings loaded once and cached for the life of the process.
    """
    return Settings()
