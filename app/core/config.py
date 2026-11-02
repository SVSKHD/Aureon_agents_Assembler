"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: Environment = Environment.DEV
    LOG_LEVEL: str = "INFO"

    # postgresql+asyncpg://user:pass@<postgres-container>:5432/spasta
    DATABASE_URL: SecretStr
    # redis://aureon-redis:6379/0
    REDIS_URL: str = "redis://aureon-redis:6379/0"
    # http://<ollama-container>:11434 over the private Docker network
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    IMAGE_AGENT_PATH: Path = Path("/opt/image-agent")
    DATA_ROOT: Path = Path("/mnt/spasta-data")
    # File that must exist on the HDD; proves the 2 TB disk is mounted, not an empty mountpoint.
    DATA_ROOT_MARKER: str = ".spasta-data-mounted"

    SECRET_KEY: SecretStr
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Readiness-probe timeout per dependency, seconds.
    HEALTH_CHECK_TIMEOUT: float = 3.0

    # --- auth ---
    SESSION_TTL_HOURS: int = Field(default=12, ge=1, le=24 * 30)
    # Secure cookies (and the __Host- prefix). Browsers drop Secure cookies over plain http
    # except on localhost, so only disable this for local http development.
    COOKIE_SECURE: bool = True
    # Failed logins allowed per (email, client IP) and per client IP within the window.
    LOGIN_MAX_FAILURES: int = Field(default=5, ge=1)
    LOGIN_MAX_FAILURES_PER_IP: int = Field(default=20, ge=1)
    LOGIN_WINDOW_SECONDS: int = Field(default=900, ge=1)

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("SECRET_KEY")
    @classmethod
    def _secret_len(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return v

    @property
    def data_marker_path(self) -> Path:
        return self.DATA_ROOT / self.DATA_ROOT_MARKER

    @property
    def session_cookie_name(self) -> str:
        return "__Host-aureon_session" if self.COOKIE_SECURE else "aureon_session"

    @property
    def csrf_cookie_name(self) -> str:
        return "__Host-aureon_csrf" if self.COOKIE_SECURE else "aureon_csrf"


@lru_cache
def get_settings() -> Settings:
    return Settings()
