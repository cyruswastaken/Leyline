"""Record and replay embeddings so retrieval evals run offline and deterministically.

A real embedding model cannot run in CI. Instead the vectors it produces for
the corpus chunks and the golden questions are recorded once into a JSON
cache (``evals/embeddings.json``); CI replays them. Hybrid retrieval quality
is then measured on the *real* model's vectors, reproducibly, with no model.

The cache is keyed by the exact text that was embedded (including any task
prefix). Change the chunker, a corpus document, a golden question, a prefix or
the model and some texts will be missing: replay then fails loudly with
instructions to re-record, instead of silently measuring something else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from array import array
from collections.abc import Sequence
from pathlib import Path

from leyline.ports.embeddings import Embedder

FORMAT_VERSION = 1


class MissingEmbeddingError(RuntimeError):
    """The cache has no vector for a text; it must be re-recorded."""


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _encode(vector: Sequence[float]) -> str:
    values = array("f", vector)
    if sys.byteorder == "big":  # the file format is little-endian float32
        values.byteswap()
    return base64.b64encode(values.tobytes()).decode("ascii")


def _decode(blob: str) -> list[float]:
    values = array("f")
    values.frombytes(base64.b64decode(blob))
    if sys.byteorder == "big":
        values.byteswap()
    return list(values)


class EmbeddingCache:
    def __init__(
        self, model: str, vectors: dict[str, list[float]] | None = None
    ) -> None:
        self.model = model
        self.vectors: dict[str, list[float]] = vectors or {}

    def __len__(self) -> int:
        return len(self.vectors)

    @classmethod
    def load(cls, path: Path) -> EmbeddingCache:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("format") != FORMAT_VERSION:
            raise ValueError(
                f"{path.name}: unsupported cache format {raw.get('format')!r}"
            )
        return cls(
            raw["model"], {key: _decode(blob) for key, blob in raw["vectors"].items()}
        )

    def save(self, path: Path) -> None:
        dimensions = {len(v) for v in self.vectors.values()}
        payload = {
            "format": FORMAT_VERSION,
            "model": self.model,
            "dimensions": dimensions.pop() if len(dimensions) == 1 else None,
            "vectors": {key: _encode(v) for key, v in sorted(self.vectors.items())},
        }
        path.write_text(
            json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8"
        )


class ReplayEmbedder:
    """Serves vectors from a cache; never calls a model."""

    def __init__(self, cache: EmbeddingCache) -> None:
        self._cache = cache

    @property
    def model(self) -> str:
        return self._cache.model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        missing = [t for t in texts if text_key(t) not in self._cache.vectors]
        if missing:
            example = missing[0][:80].replace("\n", " ")
            raise MissingEmbeddingError(
                f"{len(missing)} text(s) are not in the embedding cache (first: "
                f"{example!r}). Re-record it: python -m leyline.evals --index hybrid "
                "--seed --reindex --record-embeddings evals/embeddings.json"
            )
        return [list(self._cache.vectors[text_key(t)]) for t in texts]


class RecordingEmbedder:
    """Calls a real embedder and remembers every vector it returns."""

    def __init__(self, inner: Embedder, cache: EmbeddingCache | None = None) -> None:
        self._inner = inner
        self.cache = cache or EmbeddingCache(inner.model)
        if self.cache.model != inner.model:
            # Vectors from another model must never be mixed into one cache.
            self.cache = EmbeddingCache(inner.model)

    @property
    def model(self) -> str:
        return self._inner.model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        missing = [
            t for t in dict.fromkeys(texts) if text_key(t) not in self.cache.vectors
        ]
        if missing:
            for text, vector in zip(missing, self._inner.embed(missing), strict=True):
                self.cache.vectors[text_key(text)] = list(vector)
        return [list(self.cache.vectors[text_key(t)]) for t in texts]
