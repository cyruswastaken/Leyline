from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    """Turns text into vectors. Optional: the app runs without one."""

    @property
    def model(self) -> str:
        """Identifier recorded with every indexed version, so that changing
        the model is detected and forces a re-index."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per input text, in order. May raise on provider errors."""
        ...
