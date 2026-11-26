from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from leyline.ports.search import SearchHit


class RerankError(RuntimeError):
    """The reranker could not score the candidates (outage, timeout, bad reply,
    open circuit). The caller keeps the search order instead of failing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Scored:
    """A candidate with the reranker's relevance score (higher is better)."""

    hit: SearchHit
    score: float


class Reranker(Protocol):
    """Re-scores candidates for a query with a model that reads both together.

    It only ever sees chunks the asker may already read (it runs after the
    authorization re-check), so it can reorder or drop them but has nothing
    restricted to reveal. Implementations must return only the hits they were
    given; the caller checks that anyway.
    """

    @property
    def model(self) -> str: ...

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> list[Scored]:
        """Scores for ``hits``, in any order. Raises ``RerankError``."""
        ...
