"""Fix the embedding dimension at 768 and add an HNSW index.

Revision ID: 0002_vector_dimension_and_index
Revises: 0001_documents_and_ingestion
"""

from __future__ import annotations

from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002_vector_dimension_and_index"
down_revision = "0001_documents_and_ingestion"
branch_labels = None
depends_on = None

DIMENSIONS = 768


def upgrade() -> None:
    # Vectors of any other size cannot be stored in vector(768). Drop them and
    # clear the model recorded on their versions, so the admin page flags those
    # documents as "needs re-index" instead of silently serving stale vectors.
    op.execute(
        """
        UPDATE document_versions
           SET embedding_model = NULL, embedding_dimensions = NULL
         WHERE id IN (
               SELECT DISTINCT version_id FROM chunks
                WHERE embedding IS NOT NULL AND vector_dims(embedding) <> 768)
        """
    )
    op.execute(
        "UPDATE chunks SET embedding = NULL "
        "WHERE embedding IS NOT NULL AND vector_dims(embedding) <> 768"
    )
    op.alter_column(
        "chunks",
        "embedding",
        existing_type=Vector(),
        type_=Vector(DIMENSIONS),
        existing_nullable=True,
        postgresql_using="embedding::vector(768)",
    )
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.alter_column(
        "chunks",
        "embedding",
        existing_type=Vector(DIMENSIONS),
        type_=Vector(),
        existing_nullable=True,
        postgresql_using="embedding::vector",
    )
