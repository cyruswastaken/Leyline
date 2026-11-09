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

    # --- Identity (OpenID Connect) ---------------------------------------
    # Keycloak locally, Microsoft Entra ID on Azure: both are standard OIDC.
    oidc_metadata_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_scopes: str = "openid profile email"
    # Token claims that carry document-ACL groups and app roles.
    oidc_groups_claim: str = "groups"
    oidc_roles_claim: str = "roles"

    # --- Sessions (server-side, in Redis) ---------------------------------
    redis_url: str = "redis://localhost:6379/0"
    session_cookie_secure: bool = False
    session_idle_seconds: int = Field(default=30 * 60, gt=0)
    session_absolute_seconds: int = Field(default=12 * 60 * 60, gt=0)

    # Number of reverse proxies in front of the app whose X-Forwarded-*
    # headers are trusted (0 = trust none; the client address is the socket).
    trusted_proxy_count: int = Field(default=0, ge=0, le=5)

    # Interim bridge until per-document group ACLs arrive in Phase 2:
    # "restricted" documents require membership in one of these groups.
    # Comma-separated. "general" documents need only a signed-in user.
    restricted_access_groups: str = "security"

    max_message_length: int = Field(default=2000, gt=0)
    max_request_bytes: int = Field(default=16 * 1024, gt=0)
    rate_limit_requests: int = Field(default=10, gt=0)
    rate_limit_window_seconds: int = Field(default=60, gt=0)
    # Per client IP, applied to every request including anonymous ones.
    rate_limit_ip_requests: int = Field(default=120, gt=0)

    @property
    def llm_configured(self) -> bool:
        return bool(
            self.azure_openai_endpoint
            and self.azure_openai_api_key
            and self.azure_openai_deployment
        )

    @property
    def oidc_configured(self) -> bool:
        return bool(
            self.oidc_metadata_url and self.oidc_client_id and self.oidc_client_secret
        )

    @property
    def restricted_groups(self) -> frozenset[str]:
        return frozenset(
            g.strip() for g in self.restricted_access_groups.split(",") if g.strip()
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
            if not self.oidc_configured:
                raise ValueError(
                    "OIDC_METADATA_URL, OIDC_CLIENT_ID and "
                    "OIDC_CLIENT_SECRET are required in production"
                )
            if not self.session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE must be true in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
