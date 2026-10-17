"""Typed service settings, validated at startup (HLD §11.4: fail fast on invalid config)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Base settings every service extends. Env vars use the ``SF_`` prefix."""

    model_config = SettingsConfigDict(env_prefix="SF_", extra="forbid")

    service_name: str
    environment: str = "local"
    log_level: str = "INFO"
    port: int = 8080
