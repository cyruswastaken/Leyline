"""Embeddings through any OpenAI-compatible endpoint.

The same adapter serves Azure OpenAI (v1 API), OpenAI and local servers such
as Ollama (``http://localhost:11434/v1``): only the base URL, key and model
name differ.
"""

from __future__ import annotations

from collections.abc import Sequence

from openai import OpenAI


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int | None = None,
        timeout_seconds: float = 30,
        max_retries: int = 2,
    ) -> None:
        self._client = OpenAI(
            base_url=base_url,
            # Local servers ignore the key but the SDK requires a value.
            api_key=api_key or "not-needed",
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        self._model = model
        self._dimensions = dimensions

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._dimensions is None:
            response = self._client.embeddings.create(
                model=self._model, input=list(texts)
            )
        else:
            response = self._client.embeddings.create(
                model=self._model, input=list(texts), dimensions=self._dimensions
            )
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in ordered]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"embedding provider returned {len(vectors)} vectors "
                f"for {len(texts)} inputs"
            )
        return vectors
