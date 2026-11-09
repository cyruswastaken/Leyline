"""SearchIndex backed by Postgres full-text search.

Phase 2 provides keyword retrieval; Phase 3 adds vector similarity and rank
fusion on the same table. The ACL predicate is applied inside the query, so
unreadable chunks are never even selected.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import ColumnElement, func, literal_column, select

from leyline.auth.principal import Principal
from leyline.db.database import Database
from leyline.db.models import Chunk, Document
from leyline.ports.search import SearchHit
from leyline.services.authorization import readable_by

_WORD = re.compile(r"[a-z0-9]+")
_MAX_TERMS = 32
_ENGLISH: ColumnElement[Any] = literal_column("'english'::regconfig")


def to_or_query(text: str) -> str | None:
    """Turn free text into a tsquery that matches any of its words.

    Only ``[a-z0-9]`` tokens are kept, so user input can never inject tsquery
    operators. Returns None when nothing searchable remains.
    """
    terms = list(dict.fromkeys(_WORD.findall(text.lower())))[:_MAX_TERMS]
    return " | ".join(terms) if terms else None


class PostgresSearchIndex:
    def __init__(self, database: Database) -> None:
        self._database = database

    def search(
        self, principal: Principal, query: str, *, limit: int
    ) -> list[SearchHit]:
        tsquery_text = to_or_query(query)
        if tsquery_text is None:
            return []

        tsquery = func.to_tsquery(_ENGLISH, tsquery_text)
        rank = func.ts_rank_cd(Chunk.content_tsv, tsquery)

        statement = (
            select(
                Chunk.id,
                Chunk.document_id,
                Chunk.version_id,
                Document.title,
                Chunk.heading,
                Chunk.content,
                rank.label("score"),
            )
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Document.deleted_at.is_(None),
                # Only the current version of a document is searchable.
                Document.current_version_id == Chunk.version_id,
                Chunk.content_tsv.op("@@")(tsquery),
                readable_by(principal),
            )
            .order_by(rank.desc(), Chunk.id)
            .limit(limit)
        )

        with self._database.session() as session:
            rows = session.execute(statement).all()

        return [
            SearchHit(
                chunk_id=row.id,
                document_id=row.document_id,
                version_id=row.version_id,
                title=row.title,
                heading=row.heading,
                content=row.content,
                score=float(row.score),
            )
            for row in rows
        ]
