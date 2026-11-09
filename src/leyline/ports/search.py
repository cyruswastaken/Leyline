from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from leyline.auth.principal import Principal


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    title: str
    heading: str | None
    content: str
    # Final ordering score. Comparable only within one result list.
    score: float
    # The underlying signals, when the implementation has them.
    similarity: float | None = None  # cosine similarity of the vector leg
    lexical_score: float | None = None  # keyword rank of the full-text leg


class SearchIndex(Protocol):
    """Permission-aware retrieval.

    ``principal`` is a required argument on purpose: an unfiltered search
    cannot be written against this interface. Implementations must return
    only chunks of current, non-deleted documents the principal may read.
    """

    def search(
        self, principal: Principal, query: str, *, limit: int
    ) -> list[SearchHit]: ...
