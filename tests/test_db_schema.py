"""Schema and migration behavior against a real Postgres."""

import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

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


def test_vectors_of_any_dimension_round_trip(db):
    with db.engine.begin() as connection:
        document = _document(connection)
        version = _version(connection, document)
        for index, vector in enumerate(("[1,2,3]", "[0.5,0.25]")):
            connection.execute(
                text(
                    "INSERT INTO chunks (id, document_id, version_id, chunk_index, "
                    "content, embedding) VALUES (:id, :d, :v, :i, 'x', :vec)"
                ),
                {
                    "id": uuid.uuid4(),
                    "d": document,
                    "v": version,
                    "i": index,
                    "vec": vector,
                },
            )
        stored = (
            connection.execute(
                text("SELECT embedding::text FROM chunks ORDER BY chunk_index")
            )
            .scalars()
            .all()
        )
    assert stored == ["[1,2,3]", "[0.5,0.25]"]
