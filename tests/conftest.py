import os
import threading

import fakeredis
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from leyline.adapters.blob_memory import InMemoryBlobStore
from leyline.adapters.search_postgres import PostgresSearchIndex
from leyline.app import create_app
from leyline.db import migrate
from leyline.db.database import Database
from leyline.ingestion.service import IngestionService
from leyline.worker import run_worker
from tests.support import (  # sets the test environment first
    PERSONAS,
    TEST_DATABASE_URL,
    FakeEmbedder,
    FakeSearchIndex,
    StubDatabase,
    login_as,
    make_settings,
)

# --------------------------------------------------------------------------
# Hermetic fixtures: no database, no services. Used by most tests.
# --------------------------------------------------------------------------


@pytest.fixture
def redis():
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def search_index():
    return FakeSearchIndex()


@pytest.fixture
def app(settings, redis, llm_calls, search_index):
    flask_app = create_app(
        settings,
        redis_client=redis,
        database=StubDatabase(),
        blob_store=InMemoryBlobStore(),
        search_index=search_index,
    )
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def llm_calls(monkeypatch):
    """Replace the model call and record the context it would receive."""
    calls = []

    def fake_generate_answer(query, context, source):
        calls.append({"query": query, "context": context, "source": source})
        return "stub answer"

    monkeypatch.setattr(
        "leyline.services.chat_service.generate_answer", fake_generate_answer
    )
    return calls


@pytest.fixture
def events(monkeypatch):
    """Record every security event instead of writing it to the log file."""
    recorded = []

    def record(**kwargs):
        recorded.append(kwargs)

    for target in (
        "leyline.services.chat_service",
        "leyline.web.auth",
        "leyline.auth.access",
        "leyline.app",
    ):
        monkeypatch.setattr(f"{target}.log_security_event", record)
    return recorded


@pytest.fixture
def login(client):
    def _login(persona: str) -> str:
        return login_as(client, PERSONAS[persona])

    return _login


# --------------------------------------------------------------------------
# Integration fixtures: a real, freshly migrated Postgres (with pgvector).
# --------------------------------------------------------------------------

_TABLES = (
    "chunks, ingestion_jobs, document_acl, document_versions, documents, audit_events"
)


@pytest.fixture(scope="session")
def _test_database():
    """Create ``leyline_test`` from scratch and migrate it with Alembic.

    Using the real migrations here means every integration test also proves
    that the migrations produce a working schema.
    """
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
            )
            connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    except Exception as exc:
        message = (
            "Postgres is not reachable for integration tests. "
            "Start it with `docker compose up -d postgres` "
            f"(or set TEST_DATABASE_URL). Cause: {type(exc).__name__}"
        )
        if os.environ.get("CI") or os.environ.get("LEYLINE_REQUIRE_DB"):
            pytest.fail(message)
        pytest.skip(message)
    finally:
        admin.dispose()

    database = Database(url.render_as_string(hide_password=False), pool_size=4)
    migrate.upgrade(database.engine)
    yield database
    database.dispose()


@pytest.fixture
def db(_test_database):
    """The test database, emptied before the test."""
    with _test_database.engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    return _test_database


@pytest.fixture
def blobs():
    return InMemoryBlobStore()


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def ingestion(db, blobs, embedder, settings):
    return IngestionService(db, blobs, embedder, settings)


@pytest.fixture
def drain(db, ingestion, settings):
    """Run the worker until the queue is empty; return jobs processed."""

    def _drain() -> int:
        return run_worker(ingestion, db, settings, stop=threading.Event(), once=True)

    return _drain


@pytest.fixture
def db_app(settings, redis, llm_calls, db, blobs, embedder):
    flask_app = create_app(
        settings,
        redis_client=redis,
        database=db,
        blob_store=blobs,
        embedder=embedder,
    )
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture
def db_client(db_app):
    return db_app.test_client()


@pytest.fixture
def search(db):
    return PostgresSearchIndex(db)


@pytest.fixture
def add_doc(ingestion, drain):
    """Upload a document and (by default) index it. Returns its id."""

    def _add(title, text, groups, *, filename="doc.md", index=True):
        result = ingestion.submit_upload(
            actor_id="test-admin",
            filename=filename,
            data=text.encode(),
            title=title,
            groups=list(groups),
        )
        if index:
            drain()
        return result.document_id

    return _add


@pytest.fixture
def scratch_database():
    """A throwaway, *unmigrated* database (for migration up/down tests)."""
    name = "leyline_scratch_test"
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:
        pytest.skip(f"Postgres not reachable: {type(exc).__name__}")
    database = Database(
        url.set(database=name).render_as_string(hide_password=False), pool_size=2
    )
    yield database
    database.dispose()
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()
