from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


class ModelUnavailableError(RuntimeError):
    """The chat model could not produce an answer (outage, timeout, bad
    response, or an open circuit). Callers degrade gracefully instead of
    failing with a generic 500."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Usage:
    """Tokens a provider reported for one or more calls (zero if it reported none).

    The caller passes one in and the adapter fills it, so a model shared between
    threads never holds per-request state."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reported: bool = False  # True once a provider's own count was added

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += max(0, prompt_tokens)
        self.completion_tokens += max(0, completion_tokens)
        self.reported = True

    def merge(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reported = self.reported or other.reported


class ChatModel(Protocol):
    """Generates text from a system prompt and a user prompt. The model is
    given no tools: it can only return text."""

    @property
    def model(self) -> str: ...

    def complete(self, system: str, user: str, usage: Usage | None = None) -> str:
        """The full answer. Raises ``ModelUnavailableError``."""
        ...

    def stream(
        self, system: str, user: str, usage: Usage | None = None
    ) -> Iterator[str]:
        """The answer in pieces. Raises ``ModelUnavailableError`` (possibly
        part-way through). Closing the iterator cancels the request. When given,
        ``usage`` is filled in once the provider reports it (after the last piece)."""
        ...
