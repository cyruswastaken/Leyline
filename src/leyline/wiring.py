"""Build the concrete adapters from settings.

Shared by the web app and the worker so both see the same configuration.
Each builder returns None when its feature is not configured; callers decide
whether that is acceptable (embeddings) or an error (database).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from redis import Redis

from leyline.adapters.blob_azure import AzureBlobStore
from leyline.adapters.circuit_breaker import CircuitBreaker
from leyline.adapters.embeddings_openai import OpenAICompatibleEmbedder
from leyline.adapters.llm_openai import OpenAICompatibleChatModel
from leyline.adapters.redis_entra import EntraRedisCredentials
from leyline.adapters.rerank_http import HttpReranker
from leyline.adapters.search_postgres import PostgresSearchIndex, SearchOptions
from leyline.auth.azure_identity import OPENAI, POSTGRES, ManagedIdentity
from leyline.config import Settings
from leyline.db.database import Database
from leyline.ports.blob import BlobStore
from leyline.ports.embeddings import Embedder
from leyline.ports.llm import ChatModel
from leyline.ports.rerank import Reranker
from leyline.ports.search import SearchIndex


def configure_logging() -> None:
    """Console logging for the worker and CLI tools."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # The Azure SDK logs every HTTP request's headers at INFO.
    logging.getLogger("azure").setLevel(logging.WARNING)


def build_identity(settings: Settings) -> ManagedIdentity:
    """The container's Azure managed identity (only usable inside Azure)."""
    return ManagedIdentity(settings.azure_client_id)


def _token_provider(settings: Settings, resource: str) -> Callable[[], str]:
    identity = build_identity(settings)
    return lambda: identity.token(resource).token


def build_database(settings: Settings) -> Database | None:
    if settings.database_url is None:
        return None
    return Database(
        settings.database_url.get_secret_value(),
        password_provider=(
            _token_provider(settings, POSTGRES)
            if settings.database_entra_auth
            else None
        ),
    )


def build_redis(settings: Settings) -> Redis:
    """A Redis client for sessions and rate limits (key in the URL, or the managed
    identity when ``REDIS_ENTRA_USERNAME`` is set)."""
    options: dict[str, Any] = {
        "decode_responses": True,
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
        "health_check_interval": 30,
    }
    if settings.redis_entra_username:
        options["credential_provider"] = EntraRedisCredentials(
            build_identity(settings), settings.redis_entra_username
        )
    return Redis.from_url(settings.redis_url, **options)


def build_blob_store(settings: Settings) -> BlobStore | None:
    if settings.blob_connection_string is not None:
        return AzureBlobStore(
            settings.blob_connection_string.get_secret_value(), settings.blob_container
        )
    if settings.blob_account_url:
        return AzureBlobStore(
            None,
            settings.blob_container,
            account_url=settings.blob_account_url,
            credential=build_identity(settings),  # type: ignore[arg-type]
        )
    return None


def build_search_index(
    settings: Settings, database: Database, embedder: Embedder | None
) -> SearchIndex:
    """Hybrid when an embedder is configured, keyword-only otherwise."""
    return PostgresSearchIndex(
        database, embedder, SearchOptions.from_settings(settings)
    )


def build_chat_model(settings: Settings) -> ChatModel | None:
    endpoint = settings.llm_endpoint
    if endpoint is None:
        return None
    base_url, api_key, model = endpoint
    return OpenAICompatibleChatModel(
        base_url=base_url,
        api_key=_token_provider(settings, OPENAI)
        if settings.llm_entra_auth
        else api_key,
        model=model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        temperature=0.1 if settings.llm_send_temperature else None,
        stream_usage=settings.llm_stream_usage,
        breaker=CircuitBreaker(
            failure_threshold=settings.llm_breaker_failures,
            reset_seconds=settings.llm_breaker_reset_seconds,
        ),
    )


def build_reranker(settings: Settings) -> Reranker | None:
    """A cross-encoder reranker when ``RERANK_BASE_URL`` is set, otherwise None."""
    if not settings.rerank_base_url:
        return None
    return HttpReranker(
        base_url=settings.rerank_base_url,
        model=settings.rerank_model or "reranker",
        api_key=(
            settings.rerank_api_key.get_secret_value()
            if settings.rerank_api_key
            else ""
        ),
        timeout_seconds=settings.rerank_timeout_seconds,
    )


def build_embedder(settings: Settings) -> Embedder | None:
    base_url, model = settings.embedding_base_url, settings.embedding_model
    if not base_url or not model:
        return None
    requested_dimensions = (
        settings.embedding_dimensions if settings.embedding_send_dimensions else None
    )
    api_key: str | Callable[[], str] = (
        settings.embedding_api_key.get_secret_value()
        if settings.embedding_api_key
        else ""
    )
    if settings.embedding_entra_auth:
        api_key = _token_provider(settings, OPENAI)
    return OpenAICompatibleEmbedder(
        base_url=base_url,
        api_key=api_key,
        model=model,
        dimensions=requested_dimensions,
    )
