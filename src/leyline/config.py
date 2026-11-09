"""Typed application configuration.

All settings come from environment variables (or a local ``.env`` file) and
are validated once at startup, so misconfiguration fails fast with a clear
message instead of surfacing later on a request.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "local" and "test" tolerate a missing model configuration so the app
    # can start (and report not-ready); "production" refuses to start.
    leyline_env: Literal["local", "test", "production"] = "local"
    flask_debug: bool = False

    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_deployment: str | None = None
    llm_timeout_seconds: float = Field(default=30, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    corpus_dir: Path = REPO_ROOT / "corpus"
    log_dir: Path = REPO_ROOT / "logs"

    max_message_length: int = Field(default=2000, gt=0)
    max_request_bytes: int = Field(default=16 * 1024, gt=0)
    rate_limit_requests: int = Field(default=10, gt=0)
    rate_limit_window_seconds: int = Field(default=60, gt=0)

    @property
    def llm_configured(self) -> bool:
        return bool(
            self.azure_openai_endpoint
            and self.azure_openai_api_key
            and self.azure_openai_deployment
        )

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        if self.leyline_env == "production":
            if self.flask_debug:
                raise ValueError("FLASK_DEBUG must be off in production")
            if not self.llm_configured:
                raise ValueError(
                    "AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and "
                    "AZURE_OPENAI_DEPLOYMENT are required in production"
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
