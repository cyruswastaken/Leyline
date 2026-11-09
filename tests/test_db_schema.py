"""Schema and migration behavior against a real Postgres."""

import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from leyline.constants import EMBEDDING_DIMENSIONS
from leyline.db import migrate
from leyline.db.models import Base

EXPECTED_TABLES = {
    "alembic_version",
    "audit_events",
    "chunks",
    "document_acl",
    "document_versions",
    "documents",
    "ingestion_jobs",
}


def test_migrations_apply_from_scratch_and_roll_back_cleanly(scratch_database):
    engine = scratch_database.engine

    assert migrate.current_revision(engine) is None
    migrate.upgrade(engine)
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES
    assert migrate.is_current(engine)

    migrate.downgrade(engine, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    assert not migrate.is_current(engine)

    migrate.upgrade(engine)  # and forwards again
    assert migrate.is_current(engine)


def test_upgrading_twice_is_harmless(scratch_database):
    migrate.upgrade(scratch_database.engine)
    migrate.upgrade(scratch_database.engine)
    assert migrate.is_current(scratch_database.engine)


def test_models_and_migrations_have_not_drifted(db):
    """If a model changes without a migration (or vice versa), this fails."""
    with db.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def _document(connection, **overrides):
    values = {"id": uuid.uuid4(), "title": "t", "created_by": "u", "source_ref": None}
    values.update(overrides)
    connection.execute(
        text(
            "INSERT INTO documents (id, title, created_by, source_ref) "
            "VALUES (:id, :title, :created_by, :source_ref)"
        ),
        values,
    )
    return values["id"]


def _version(connection, document_id, *, number=1, status="pending", sha="x"):
    version_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO document_versions (id, document_id, version, filename, "
            "content_type, size_bytes, sha256, blob_key, status, uploaded_by) "
            "VALUES (:id, :d, :n, 'f', 'text/plain', 1, :sha, 'k', :status, 'u')"
        ),
        {"id": version_id, "d": document_id, "n": number, "sha": sha, "status": status},
    )
    return version_id


def test_version_status_is_constrained(db):
    with (
        pytest.raises(DBAPIError, match="ck_document_versions_status"),
        db.engine.begin() as connection,
    ):
        _version(connection, _document(connection), status="bogus")


def test_a_document_cannot_have_two_versions_with_the_same_number(db):
    with (
        pytest.raises(DBAPIError, match="uq_document_versions_number"),
        db.engine.begin() as connection,
    ):
        document = _document(connection)
        _version(connection, document, number=1)
        _version(connection, document, number=1)


def test_an_acl_entry_cannot_be_empty(db):
    with (
        pytest.raises(DBAPIError, match="ck_document_acl_group_not_empty"),
        db.engine.begin() as connection,
    ):
        document = _document(connection)
        connection.execute(
            text("INSERT INTO document_acl (document_id, group_id) VALUES (:d, '')"),
            {"d": document},
        )


def test_source_refs_are_unique(db):
    with (
        pytest.raises(DBAPIError, match="uq_documents_source_ref"),
        db.engine.begin() as connection,
    ):
        _document(connection, source_ref="GEN-001")
        _document(connection, source_ref="GEN-001")


def test_job_kind_and_state_are_constrained(db):
    with db.engine.begin() as connection:
        document = _document(connection)
    for column, value in (("kind", "bogus"), ("state", "bogus")):
        values = {"kind": "ingest", "state": "queued", column: value}
        with (
            pytest.raises(DBAPIError, match=f"ck_ingestion_jobs_{column}"),
            db.engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "INSERT INTO ingestion_jobs (id, kind, document_id, state, max_attempts) "
                    "VALUES (:id, :kind, :d, :state, 3)"
                ),
                {"id": uuid.uuid4(), "d": document, **values},
            )


def test_deleting_a_document_row_cascades_to_everything_it_owns(db):
    with db.engine.begin() as connection:
        document = _document(connection)
        version = _version(connection, document)
        connection.execute(
            text(
                "INSERT INTO document_acl (document_id, group_id) VALUES (:d, 'staff')"
            ),
            {"d": document},
        )
        connection.execute(
            text(
                "INSERT INTO chunks (id, document_id, version_id, chunk_index, content) "
                "VALUES (:id, :d, :v, 0, 'hello world')"
            ),
            {"id": uuid.uuid4(), "d": document, "v": version},
        )
        connection.execute(
            text(
                "INSERT INTO ingestion_jobs (id, kind, document_id, version_id, state, "
                "max_attempts) VALUES (:id, 'ingest', :d, :v, 'queued', 3)"
            ),
            {"id": uuid.uuid4(), "d": document, "v": version},
        )

    with db.engine.begin() as connection:
        # the circular current_version FK must not block deleting the document
        connection.execute(
            text("UPDATE documents SET current_version_id = :v WHERE id = :d"),
            {"v": version, "d": document},
        )
        connection.execute(text("DELETE FROM documents WHERE id = :d"), {"d": document})
        for table in ("document_versions", "document_acl", "chunks", "ingestion_jobs"):
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            assert count == 0, table


def test_full_text_column_is_generated_from_heading_and_content(db):
    with db.engine.begin() as connection:
        document = _document(connection)
        version = _version(connection, document)
        connection.execute(
            text(
                "INSERT INTO chunks (id, document_id, version_id, chunk_index, heading, "
                "content) VALUES (:id, :d, :v, 0, 'Remote access', 'Install the client')"
            ),
            {"id": uuid.uuid4(), "d": document, "v": version},
        )
        matches = connection.execute(
            text(
                "SELECT count(*) FROM chunks WHERE content_tsv @@ "
                "to_tsquery('english', 'remote & install')"
            )
        ).scalar()
    assert matches == 1  # words from the heading and the body both match


def _vector_literal(size: int, value: float = 0.5) -> str:
    return "[" + ",".join([str(value)] * size) + "]"


def _insert_chunk(connection, document, version, index, vector):
    connection.execute(
        text(
            "INSERT INTO chunks (id, document_id, version_id, chunk_index, "
            "content, embedding) VALUES (:id, :d, :v, :i, 'x', :vec)"
        ),
        {"id": uuid.uuid4(), "d": document, "v": version, "i": index, "vec": vector},
    )


def test_vectors_have_exactly_the_configured_dimension(db):
    with db.engine.begin() as connection:
        document = _document(connection)
        version = _version(connection, document)
        _insert_chunk(
            connection, document, version, 0, _vector_literal(EMBEDDING_DIMENSIONS)
        )
        stored = connection.execute(
            text("SELECT vector_dims(embedding) FROM chunks")
        ).scalar()
    assert stored == EMBEDDING_DIMENSIONS == 768


@pytest.mark.parametrize("size", [3, 767, 769, 1536])
def test_vectors_of_any_other_size_are_rejected(db, size):
    with (
        pytest.raises(DBAPIError, match="expected 768 dimensions"),
        db.engine.begin() as connection,
    ):
        document = _document(connection)
        version = _version(connection, document)
        _insert_chunk(connection, document, version, 0, _vector_literal(size))


def test_chunks_without_an_embedding_are_still_allowed(db):
    with db.engine.begin() as connection:
        document = _document(connection)
        version = _version(connection, document)
        connection.execute(
            text(
                "INSERT INTO chunks (id, document_id, version_id, chunk_index, content) "
                "VALUES (:id, :d, :v, 0, 'keyword only')"
            ),
            {"id": uuid.uuid4(), "d": document, "v": version},
        )
        assert connection.execute(text("SELECT count(*) FROM chunks")).scalar() == 1


def test_an_hnsw_cosine_index_backs_vector_search(db):
    with db.engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunks_embedding_hnsw'"
            )
        ).scalar()
    assert definition is not None
    assert "USING hnsw" in definition and "vector_cosine_ops" in definition


def test_migration_0002_drops_stale_vectors_and_flags_their_versions(scratch_database):
    """Phase 2 allowed vectors of any size; upgrading must not leave unusable ones."""
    engine = scratch_database.engine
    migrate.upgrade(engine, "0001_documents_and_ingestion")
    document, version = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO documents (id, title, created_by) VALUES (:d, 't', 'u')"),
            {"d": document},
        )
        connection.execute(
            text(
                "INSERT INTO document_versions (id, document_id, version, filename, "
                "content_type, size_bytes, sha256, blob_key, status, uploaded_by, "
                "embedding_model, embedding_dimensions) VALUES (:v, :d, 1, 'f', "
                "'text/plain', 1, 'x', 'k', 'indexed', 'u', 'old-model', 3)"
            ),
            {"v": version, "d": document},
        )
        _insert_chunk(connection, document, version, 0, "[1,2,3]")

    migrate.upgrade(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT embedding FROM chunks")).scalar() is None
        flagged = connection.execute(
            text("SELECT embedding_model, embedding_dimensions FROM document_versions")
        ).one()
        assert tuple(flagged) == (None, None)  # shows up as "needs re-index"
        column = connection.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
            )
        ).scalar()
    assert column == "vector(768)"
    migrate.downgrade(engine, "0001_documents_and_ingestion")  # and it is reversible
    assert not migrate.is_current(engine)
