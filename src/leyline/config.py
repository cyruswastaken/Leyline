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

from leyline.constants import EMBEDDING_DIMENSIONS

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors must never echo configuration values (secrets).
        hide_input_in_errors=True,
    )

    # "local" and "test" tolerate a missing model configuration so the app
    # can start (and report not-ready); "production" refuses to start.
    leyline_env: Literal["local", "test", "production"] = "local"
    flask_debug: bool = False

    # Chat model: any OpenAI-compatible endpoint (Ollama, OpenAI, Azure OpenAI v1).
    # LLM_BASE_URL / LLM_MODEL / LLM_API_KEY take precedence; the AZURE_OPENAI_*
    # trio is still understood and maps to ``<endpoint>/openai/v1/``.
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_deployment: str | None = None
    llm_timeout_seconds: float = Field(default=30, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    # Consecutive failures before the circuit opens, and how long it stays open.
    llm_breaker_failures: int = Field(default=5, gt=0)
    llm_breaker_reset_seconds: float = Field(default=30, gt=0)

    corpus_dir: Path = REPO_ROOT / "corpus"

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

    # --- Database (Postgres + pgvector) ------------------------------------
    # postgresql+pg8000://user:password@host:5432/dbname
    database_url: SecretStr | None = None

    # --- Document storage (Azure Blob Storage / Azurite) ---------------------
    blob_connection_string: SecretStr | None = None
    blob_container: str = "leyline-documents"

    # --- Uploads and parsing ------------------------------------------------
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_pdf_pages: int = Field(default=300, gt=0)
    # Guards against zip bombs: total uncompressed size of a DOCX archive.
    max_docx_uncompressed_bytes: int = Field(default=50 * 1024 * 1024, gt=0)

    # --- Embeddings (any OpenAI-compatible endpoint: Azure OpenAI v1, OpenAI,
    # Ollama, ...). Optional: without it documents are indexed for keyword
    # search only and can be re-indexed later once a model is configured.
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None
    # Must equal the schema's fixed vector size (see leyline.constants).
    embedding_dimensions: int = EMBEDDING_DIMENSIONS
    # Ask the API to truncate to ``embedding_dimensions`` (OpenAI text-embedding-3
    # and Azure OpenAI support this; most local models do not).
    embedding_send_dimensions: bool = False
    # Asymmetric models need task prefixes, e.g. nomic-embed-text uses
    # "search_query: " and "search_document: ". Changing a prefix invalidates
    # stored vectors: re-index every document afterwards.
    embedding_query_prefix: str = ""
    embedding_document_prefix: str = ""
    embedding_batch_size: int = Field(default=32, gt=0, le=256)

    # --- Ingestion worker -----------------------------------------------------
    ingest_max_attempts: int = Field(default=5, gt=0, le=20)
    ingest_backoff_seconds: int = Field(default=30, ge=0)
    # A job running longer than this is presumed dead and may be reclaimed.
    ingest_lease_seconds: int = Field(default=300, gt=0)
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    # When set, the worker touches this file every loop; a container health
    # check treats a stale file as a hung worker.
    worker_heartbeat_path: Path | None = None
    chunk_target_words: int = Field(default=200, gt=0)
    chunk_overlap_words: int = Field(default=40, ge=0)

    search_top_k: int = Field(default=5, gt=0, le=50)
    # Hybrid retrieval. Each leg (keyword, vector) proposes this many chunks,
    # which are fused by reciprocal rank (rrf_k damps the head of each list).
    search_candidates: int = Field(default=40, gt=0, le=200)
    search_rrf_k: int = Field(default=60, gt=0)
    # Relevance floors on the underlying signals (fused ranks are relative and
    # cannot say "nothing relevant"): cosine similarity for the vector leg and
    # ts_rank_cd for the keyword leg. 0 disables a floor.
    # Defaults were calibrated for nomic-embed-text (with its task prefixes) on
    # the golden set: `python -m leyline.evals --index hybrid --sweep`. Another
    # model has a different similarity scale, so re-run the sweep after
    # changing it. The lexical floor applies only while the vector leg runs.
    search_min_similarity: float = Field(default=0.58, ge=-1.0, le=1.0)
    search_min_lexical: float = Field(default=0.5, ge=0.0)
    # At most this many chunks of one document in a result list.
    search_max_chunks_per_document: int = Field(default=2, gt=0)

    # --- Chat ----------------------------------------------------------------
    # Rough token budget for retrieved text in one prompt (about 4 chars/token).
    llm_context_token_budget: int = Field(default=3000, gt=200)
    # How many of the user's earlier questions are used to rewrite a follow-up.
    chat_history_messages: int = Field(default=4, ge=0, le=20)
    # Conversations idle longer than this are deleted for good; 0 keeps them.
    conversation_retention_days: int = Field(default=0, ge=0)
    # A message scoring at least this on injection signals is refused (403).
    injection_block_score: int = Field(default=4, gt=0)

    # --- Telemetry (OpenTelemetry) --------------------------------------------
    # "none" is a no-op; "console" prints spans/metrics; "otlp" ships them to a
    # collector (needs: pip install opentelemetry-exporter-otlp-proto-http).
    otel_exporter: Literal["none", "console", "otlp"] = "none"
    otel_service_name: str = "leyline"
    otel_otlp_endpoint: str | None = None

    # --- Frontend -------------------------------------------------------------
    # Local development only: serve scripts and styles from the Vite dev server
    # (for example http://localhost:5173) for hot reload. Rejected in production.
    frontend_dev_server: str | None = None

    # --- Tickets ---------------------------------------------------------------
    ticket_max_subject: int = Field(default=150, gt=0)
    ticket_max_description: int = Field(default=5000, gt=0)
    ticket_max_comment: int = Field(default=2000, gt=0)
    # New tickets per user per rate-limit window (comments use the chat limit).
    ticket_rate_limit: int = Field(default=5, gt=0)

    max_message_length: int = Field(default=2000, gt=0)
    max_request_bytes: int = Field(default=16 * 1024, gt=0)
    rate_limit_requests: int = Field(default=10, gt=0)
    rate_limit_window_seconds: int = Field(default=60, gt=0)
    # Per client IP, applied to every request including anonymous ones.
    rate_limit_ip_requests: int = Field(default=120, gt=0)

    @property
    def llm_endpoint(self) -> tuple[str, str, str] | None:
        """(base_url, api_key, model) for the chat model, or None."""
        if self.llm_base_url and self.llm_model:
            key = self.llm_api_key.get_secret_value() if self.llm_api_key else ""
            return self.llm_base_url, key, self.llm_model
        if (
            self.azure_openai_endpoint
            and self.azure_openai_api_key
            and self.azure_openai_deployment
        ):
            return (
                f"{self.azure_openai_endpoint.rstrip('/')}/openai/v1/",
                self.azure_openai_api_key.get_secret_value(),
                self.azure_openai_deployment,
            )
        return None

    @property
    def llm_configured(self) -> bool:
        return self.llm_endpoint is not None

    @property
    def oidc_configured(self) -> bool:
        return bool(
            self.oidc_metadata_url and self.oidc_client_id and self.oidc_client_secret
        )

    @property
    def database_configured(self) -> bool:
        return self.database_url is not None

    @property
    def blob_configured(self) -> bool:
        return self.blob_connection_string is not None

    @property
    def embeddings_configured(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_model)

    @model_validator(mode="after")
    def _check_embedding_dimensions(self) -> Settings:
        if self.embedding_dimensions != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"EMBEDDING_DIMENSIONS must be {EMBEDDING_DIMENSIONS}: the database "
                "schema stores fixed-size vectors (change it with a migration)"
            )
        return self

    @model_validator(mode="after")
    def _check_chunking(self) -> Settings:
        # Otherwise the chunker could never advance through a document.
        if self.chunk_overlap_words >= self.chunk_target_words:
            raise ValueError(
                "CHUNK_OVERLAP_WORDS must be smaller than CHUNK_TARGET_WORDS"
            )
        return self

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        if self.leyline_env == "production":
            if self.flask_debug:
                raise ValueError("FLASK_DEBUG must be off in production")
            if self.frontend_dev_server:
                raise ValueError("FRONTEND_DEV_SERVER must not be set in production")
            if not self.llm_configured:
                raise ValueError(
                    "LLM_BASE_URL and LLM_MODEL (or AZURE_OPENAI_ENDPOINT, "
                    "AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT) are "
                    "required in production"
                )
            if not self.oidc_configured:
                raise ValueError(
                    "OIDC_METADATA_URL, OIDC_CLIENT_ID and "
                    "OIDC_CLIENT_SECRET are required in production"
                )
            if not (self.database_configured and self.blob_configured):
                raise ValueError(
                    "DATABASE_URL and BLOB_CONNECTION_STRING are required in production"
                )
            if not self.session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE must be true in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
