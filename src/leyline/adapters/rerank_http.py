"""A cross-encoder reranker behind a ``/rerank`` HTTP endpoint.

Speaks the protocol of Hugging Face Text Embeddings Inference (TEI), which serves
models such as ``BAAI/bge-reranker-base`` on CPU from a container:

    POST {base_url}/rerank   {"query": "...", "texts": ["...", ...]}
    -> [{"index": 0, "score": 0.98}, ...]

Scores come back as probabilities (0 to 1), so one floor means the same thing for
every query. Failures raise ``RerankError`` and are guarded by a circuit breaker,
so a slow or dead reranker costs a few seconds once and then nothing.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

import requests

from leyline.adapters.circuit_breaker import CircuitBreaker
from leyline.ports.rerank import RerankError, Scored
from leyline.ports.search import SearchHit

logger = logging.getLogger(__name__)

# Characters of a chunk sent to the model. Cross-encoders read about 512 tokens in
# total, so more than this only slows the call down.
MAX_CHARS_PER_CHUNK = 1500


class HttpReranker:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 3.0,
        breaker: CircuitBreaker | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/rerank"
        self._model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout_seconds
        self._breaker = breaker or CircuitBreaker()
        self._session = session or requests.Session()

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def _text(hit: SearchHit) -> str:
        title = f"{hit.title}\n" if hit.title else ""
        heading = f"{hit.heading}\n" if hit.heading else ""
        return (title + heading + hit.content)[:MAX_CHARS_PER_CHUNK]

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> list[Scored]:
        if not hits:
            return []
        if not self._breaker.allow():
            raise RerankError("circuit_open")
        try:
            response = self._session.post(
                self._url,
                json={"query": query, "texts": [self._text(h) for h in hits]},
                headers=self._headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            rows = response.json()
            scored = self._parse(rows, hits)
        except RerankError:
            self._breaker.record_failure()
            raise
        except requests.ConnectionError as exc:
            # Could not reach the server (this includes a connect timeout).
            raise self._failed("connection_error", exc) from exc
        except requests.Timeout as exc:
            raise self._failed("timeout", exc) from exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            raise self._failed(f"http_{status}", exc) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise self._failed("bad_response", exc) from exc
        self._breaker.record_success()
        return scored

    def _failed(self, reason: str, exc: Exception) -> RerankError:
        self._breaker.record_failure()
        # The reply can echo request text; log the class and reason only.
        logger.warning("rerank call failed: %s (%s)", reason, type(exc).__name__)
        return RerankError(reason)

    @staticmethod
    def _parse(rows: object, hits: Sequence[SearchHit]) -> list[Scored]:
        if not isinstance(rows, list):
            raise RerankError("bad_response")
        scored: dict[int, float] = {}
        for row in rows:
            index, score = int(row["index"]), float(row["score"])
            if not 0 <= index < len(hits) or not math.isfinite(score):
                raise RerankError("bad_response")
            scored[index] = score
        if len(scored) != len(hits):
            raise RerankError("bad_response")
        return [Scored(hits[i], s) for i, s in sorted(scored.items())]
