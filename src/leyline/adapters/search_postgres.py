"""SearchIndex backed by Postgres: full-text, vector similarity, or both.

Two retrieval legs run over the same ``chunks`` table:

* **keyword** - ``ts_rank_cd`` over the generated full-text column
* **semantic** - cosine similarity over the pgvector embedding (HNSW index)

Each leg applies the ACL predicate (``readable_by``) *inside its own query*,
so an unreadable chunk can never enter either candidate list. The lists are
then fused with reciprocal-rank fusion. Because fused scores are relative and
cannot express "nothing here is relevant", relevance is gated on the
underlying signals instead: a chunk is only a candidate if its cosine
similarity (or keyword rank) clears a configured floor.

Without an embedder this degrades to keyword search, and if the embedding
service fails at query time the keyword leg still answers.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Row, func, literal_column, select, text
from sqlalchemy.orm import Session

from leyline.auth.principal import Principal
from leyline.config import Settings
from leyline.constants import EMBEDDING_DIMENSIONS
from leyline.db.database import Database
from leyline.db.models import Chunk, Document
from leyline.ports.embeddings import Embedder
from leyline.ports.search import SearchHit
from leyline.services.authorization import readable_by

log = logging.getLogger("leyline.search")

_WORD = re.compile(r"[a-z0-9]+")
_MAX_TERMS = 32
_ENGLISH: ColumnElement[Any] = literal_column("'english'::regconfig")
# Ties must break the same way on every run. Chunk ids are random UUIDs, so
# order by something stable instead: a result list that reshuffles equal
# scores would make evaluations (and users' answers) irreproducible.
_STABLE_ORDER = (Document.title, Chunk.chunk_index)


def to_or_query(text: str) -> str | None:
    """Turn free text into a tsquery that matches any of its words.

    Only ``[a-z0-9]`` tokens are kept, so user input can never inject tsquery
    operators. Returns None when nothing searchable remains.
    """
    terms = list(dict.fromkeys(_WORD.findall(text.lower())))[:_MAX_TERMS]
    return " | ".join(terms) if terms else None


@dataclass(frozen=True)
class SearchOptions:
    candidates: int = 40
    rrf_k: int = 60
    min_similarity: float = 0.0
    min_lexical: float = 0.0
    max_chunks_per_document: int = 2
    query_prefix: str = ""
    # Production degrades to keyword search when query embedding fails. An
    # evaluation must not: it would silently measure the wrong thing.
    strict_embedding: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> SearchOptions:
        return cls(
            candidates=settings.search_candidates,
            rrf_k=settings.search_rrf_k,
            min_similarity=settings.search_min_similarity,
            min_lexical=settings.search_min_lexical,
            max_chunks_per_document=settings.search_max_chunks_per_document,
            query_prefix=settings.embedding_query_prefix,
        )


@dataclass
class _Candidate:
    row: Row[Any]
    lexical: float | None = None
    similarity: float | None = None
    fused: float = 0.0


class PostgresSearchIndex:
    def __init__(
        self,
        database: Database,
        embedder: Embedder | None = None,
        options: SearchOptions | None = None,
    ) -> None:
        self._database = database
        self._embedder = embedder
        self._options = options or SearchOptions()

    # ------------------------------------------------------------------ #

    def search(
        self, principal: Principal, query: str, *, limit: int
    ) -> list[SearchHit]:
        options = self._options
        query_vector = self._embed_query(query)

        with self._database.session() as session:
            # The lexical floor keeps weak keyword noise out of a *hybrid* result.
            # With no vector leg (no embedder, or it failed) keywords are all we
            # have, so the floor must not throw them away.
            lexical_floor = options.min_lexical if query_vector is not None else 0.0
            lexical = self._lexical(session, principal, query, lexical_floor)
            semantic = (
                self._semantic(session, principal, query_vector)
                if query_vector is not None
                else []
            )

        fused = self._fuse(lexical, semantic)
        return self._to_hits(fused, limit, options.max_chunks_per_document)

    # -- the two legs --------------------------------------------------- #

    def _base(self, principal: Principal) -> list[ColumnElement[bool]]:
        """Conditions every leg must apply. The ACL predicate lives here."""
        return [
            Document.deleted_at.is_(None),
            # Only the current version of a document is searchable.
            Document.current_version_id == Chunk.version_id,
            readable_by(principal),
        ]

    _COLUMNS = (
        Chunk.id,
        Chunk.document_id,
        Chunk.version_id,
        Document.title,
        Chunk.heading,
        Chunk.content,
        Chunk.chunk_index,
    )

    def _lexical(
        self, session: Session, principal: Principal, query: str, floor: float
    ) -> list[tuple[Row[Any], float]]:
        tsquery_text = to_or_query(query)
        if tsquery_text is None:
            return []
        tsquery = func.to_tsquery(_ENGLISH, tsquery_text)
        rank = func.ts_rank_cd(Chunk.content_tsv, tsquery)

        statement = (
            select(*self._COLUMNS, rank.label("rank"))
            .join(Document, Document.id == Chunk.document_id)
            .where(*self._base(principal), Chunk.content_tsv.op("@@")(tsquery))
            .order_by(rank.desc(), *_STABLE_ORDER)
            .limit(self._options.candidates)
        )
        return [
            (row, float(row.rank))
            for row in session.execute(statement)
            if row.rank >= floor
        ]

    def _semantic(
        self, session: Session, principal: Principal, vector: list[float]
    ) -> list[tuple[Row[Any], float]]:
        distance = Chunk.embedding.cosine_distance(vector)
        statement = (
            select(*self._COLUMNS, distance.label("distance"))
            .join(Document, Document.id == Chunk.document_id)
            .where(*self._base(principal), Chunk.embedding.is_not(None))
            .order_by(distance, *_STABLE_ORDER)
            .limit(self._options.candidates)
        )
        self._allow_filtered_ann_scan(session)
        floor = self._options.min_similarity
        results = []
        for row in session.execute(statement):
            similarity = 1.0 - float(row.distance)
            if similarity >= floor:
                results.append((row, similarity))
        return results

    def _allow_filtered_ann_scan(self, session: Session) -> None:
        """Let the HNSW index keep scanning until enough *readable* rows are found.

        Without this, a selective permission filter applied after an ANN scan
        could leave a user with too few results (never more than they may
        read; this is about recall, not leakage). Needs pgvector >= 0.8.
        """
        try:
            with session.begin_nested():
                session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
                session.execute(
                    text(f"SET LOCAL hnsw.ef_search = {int(self._options.candidates)}")
                )
        except Exception:
            log.debug("pgvector iterative scan is unavailable; continuing without it")

    # -- query embedding ------------------------------------------------- #

    def _embed_query(self, query: str) -> list[float] | None:
        if self._embedder is None or not query.strip():
            return None
        try:
            [vector] = self._embedder.embed([self._options.query_prefix + query])
        except Exception as exc:
            if self._options.strict_embedding:
                raise
            # Never fail a question because the embedding service is down.
            log.warning(
                "query embedding failed (%s); using keyword search only",
                type(exc).__name__,
            )
            return None
        if len(vector) != EMBEDDING_DIMENSIONS:
            if self._options.strict_embedding:
                raise ValueError(
                    f"embedding model returned {len(vector)} dimensions, "
                    f"expected {EMBEDDING_DIMENSIONS}"
                )
            log.warning(
                "embedding model returned %d dimensions, expected %d; "
                "using keyword search only",
                len(vector),
                EMBEDDING_DIMENSIONS,
            )
            return None
        return vector

    # -- fusion ----------------------------------------------------------- #

    def _fuse(
        self,
        lexical: list[tuple[Row[Any], float]],
        semantic: list[tuple[Row[Any], float]],
    ) -> list[_Candidate]:
        k = self._options.rrf_k
        candidates: dict[uuid.UUID, _Candidate] = {}

        for rank, (row, score) in enumerate(lexical, start=1):
            candidate = candidates.setdefault(row.id, _Candidate(row))
            candidate.lexical = score
            candidate.fused += 1.0 / (k + rank)
        for rank, (row, similarity) in enumerate(semantic, start=1):
            candidate = candidates.setdefault(row.id, _Candidate(row))
            candidate.similarity = similarity
            candidate.fused += 1.0 / (k + rank)

        return sorted(
            candidates.values(),
            key=lambda c: (
                -c.fused,
                -(c.similarity if c.similarity is not None else -1.0),
                -(c.lexical if c.lexical is not None else -1.0),
                c.row.title,
                c.row.chunk_index,
            ),
        )

    @staticmethod
    def _to_hits(
        ranked: list[_Candidate], limit: int, per_document: int
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        taken: dict[uuid.UUID, int] = {}
        for candidate in ranked:
            row = candidate.row
            if taken.get(row.document_id, 0) >= per_document:
                continue
            taken[row.document_id] = taken.get(row.document_id, 0) + 1
            hits.append(
                SearchHit(
                    chunk_id=row.id,
                    document_id=row.document_id,
                    version_id=row.version_id,
                    title=row.title,
                    heading=row.heading,
                    content=row.content,
                    score=candidate.fused,
                    similarity=candidate.similarity,
                    lexical_score=candidate.lexical,
                )
            )
            if len(hits) >= limit:
                break
        return hits
