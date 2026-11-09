"""Typed accessors for per-application state registered by ``create_app``."""

from __future__ import annotations

from typing import cast

from flask import current_app
from redis import Redis

from leyline.config import Settings
from leyline.db.database import Database
from leyline.ingestion.service import IngestionService
from leyline.ports.blob import BlobStore
from leyline.ports.embeddings import Embedder
from leyline.ports.search import SearchIndex
from leyline.ratelimit import RedisRateLimiter

SETTINGS_KEY = "leyline.settings"
REDIS_KEY = "leyline.redis"
RATE_LIMITER_KEY = "leyline.rate_limiter"
DATABASE_KEY = "leyline.database"
BLOB_KEY = "leyline.blob_store"
EMBEDDER_KEY = "leyline.embedder"
SEARCH_KEY = "leyline.search_index"
INGESTION_KEY = "leyline.ingestion"


class NotConfiguredError(RuntimeError):
    """A feature the request needs has no configuration (answered with 503)."""


def app_settings() -> Settings:
    return cast(Settings, current_app.extensions[SETTINGS_KEY])


def redis_client() -> Redis:
    return cast(Redis, current_app.extensions[REDIS_KEY])


def rate_limiter() -> RedisRateLimiter:
    return cast(RedisRateLimiter, current_app.extensions[RATE_LIMITER_KEY])


def _get(key: str, what: str) -> object:
    value = current_app.extensions.get(key)
    if value is None:
        raise NotConfiguredError(f"{what} is not configured")
    return value


def database() -> Database:
    return cast(Database, _get(DATABASE_KEY, "the database"))


def blob_store() -> BlobStore:
    return cast(BlobStore, _get(BLOB_KEY, "blob storage"))


def search_index() -> SearchIndex:
    return cast(SearchIndex, _get(SEARCH_KEY, "search"))


def ingestion_service() -> IngestionService:
    return cast(IngestionService, _get(INGESTION_KEY, "document ingestion"))


def embedder() -> Embedder | None:
    return cast("Embedder | None", current_app.extensions.get(EMBEDDER_KEY))
