"""Build the concrete adapters from settings.

Shared by the web app and the worker so both see the same configuration.
Each builder returns None when its feature is not configured; callers decide
whether that is acceptable (embeddings) or an error (database).
"""

from __future__ import annotations

import logging

from leyline.adapters.blob_azure import AzureBlobStore
from leyline.adapters.embeddings_openai import OpenAICompatibleEmbedder
from leyline.adapters.search_postgres import PostgresSearchIndex, SearchOptions
from leyline.config import Settings
from leyline.db.database import Database
from leyline.ports.blob import BlobStore
from leyline.ports.embeddings import Embedder
from leyline.ports.search import SearchIndex


def configure_logging() -> None:
    """Console logging for the worker and CLI tools."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # The Azure SDK logs every HTTP request's headers at INFO.
    logging.getLogger("azure").setLevel(logging.WARNING)


def build_database(settings: Settings) -> Database | None:
    if settings.database_url is None:
        return None
    return Database(settings.database_url.get_secret_value())


def build_blob_store(settings: Settings) -> BlobStore | None:
    if settings.blob_connection_string is None:
        return None
    return AzureBlobStore(
        settings.blob_connection_string.get_secret_value(), settings.blob_container
    )


def build_search_index(
    settings: Settings, database: Database, embedder: Embedder | None
) -> SearchIndex:
    """Hybrid when an embedder is configured, keyword-only otherwise."""
    return PostgresSearchIndex(
        database, embedder, SearchOptions.from_settings(settings)
    )


def build_embedder(settings: Settings) -> Embedder | None:
    base_url, model = settings.embedding_base_url, settings.embedding_model
    if not base_url or not model:
        return None
    requested_dimensions = (
        settings.embedding_dimensions if settings.embedding_send_dimensions else None
    )
    return OpenAICompatibleEmbedder(
        base_url=base_url,
        api_key=(
            settings.embedding_api_key.get_secret_value()
            if settings.embedding_api_key
            else ""
        ),
        model=model,
        dimensions=requested_dimensions,
    )
