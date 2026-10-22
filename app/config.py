from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from CREDITS_* environment variables (or .env)."""

    model_config = SettingsConfigDict(env_prefix="CREDITS_", env_file=".env", extra="ignore")

    mongo_url: str = "mongodb://localhost:27017/?directConnection=true"
    mongo_db: str = "credits"
    mongo_timeout_ms: int = 5000
    redis_url: str = "redis://localhost:6379/0"
    task_max_retries: int = 5
    log_level: str = "INFO"
    # Comma-separated service API keys; several allow zero-downtime rotation.
    # Empty means every authenticated route is rejected (fail closed).
    api_keys: SecretStr = SecretStr("")

    def api_key_set(self) -> frozenset[str]:
        return frozenset(
            k.strip() for k in self.api_keys.get_secret_value().split(",") if k.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
