"""Apply a reranker to authorized search hits, safely.

Runs after the authorization re-check, so every candidate is already readable by
the asker. The reranker may reorder them and, with a floor, drop the ones it judges
unrelated (which turns a weak match into an honest "not found"). It can never add a
chunk: anything it returns that was not a candidate is ignored. If it fails, the
search order stands.
"""

from __future__ import annotations

from collections.abc import Sequence

from leyline.ports.rerank import Reranker, RerankError
from leyline.ports.search import SearchHit


def rerank_hits(
    reranker: Reranker,
    query: str,
    hits: Sequence[SearchHit],
    *,
    keep: int,
    min_score: float = 0.0,
) -> tuple[list[SearchHit], str | None]:
    """The best ``keep`` hits by reranker score (at least ``min_score``), and the
    failure reason if the reranker could not be used (then the first ``keep`` of
    ``hits`` in search order are returned)."""
    try:
        scored = reranker.rerank(query, hits)
    except RerankError as exc:
        return list(hits[:keep]), exc.reason

    allowed = {hit.chunk_id for hit in hits}
    best: dict[object, tuple[float, int, SearchHit]] = {}
    for position, item in enumerate(scored):
        if item.hit.chunk_id not in allowed:
            continue  # never add a chunk the search did not return
        known = best.get(item.hit.chunk_id)
        if known is None or item.score > known[0]:
            # Keep the search's own object, not whatever the reranker handed back.
            original = next(h for h in hits if h.chunk_id == item.hit.chunk_id)
            best[item.hit.chunk_id] = (item.score, position, original)
    # Highest score first; ties keep the search order.
    order = {hit.chunk_id: i for i, hit in enumerate(hits)}
    ranked = sorted(best.values(), key=lambda row: (-row[0], order[row[2].chunk_id]))
    return [hit for score, _, hit in ranked if score >= min_score][:keep], None
