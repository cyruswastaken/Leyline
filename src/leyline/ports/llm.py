from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol


class ModelUnavailableError(RuntimeError):
    """The chat model could not produce an answer (outage, timeout, bad
    response, or an open circuit). Callers degrade gracefully instead of
    failing with a generic 500."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ChatModel(Protocol):
    """Generates text from a system prompt and a user prompt. The model is
    given no tools: it can only return text."""

    @property
    def model(self) -> str: ...

    def complete(self, system: str, user: str) -> str:
        """The full answer. Raises ``ModelUnavailableError``."""
        ...

    def stream(self, system: str, user: str) -> Iterator[str]:
        """The answer in pieces. Raises ``ModelUnavailableError`` (possibly
        part-way through). Closing the iterator cancels the request."""
        ...
