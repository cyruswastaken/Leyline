"""Record and replay a chat model's answers, so answer evals can run in CI.

A live model cannot run in CI, but everything Leyline does *around* it can: build
the context from retrieved chunks, assemble the prompt, redact credentials, verify
citations, apply the refusal rules. The model's raw replies are recorded once from
a live run into ``evals/answers.azure.json`` and replayed here, keyed by a hash of
the exact system and user prompt it was given.

That makes the cache a tripwire: change the retrieval, the chunker, the context
builder or either prompt and the prompt text changes, so replay fails loudly with
instructions to re-record instead of silently grading something else. What it
cannot do is judge a *different* model; that stays a manual run (docs/EVALUATION.md).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

from leyline.ports.llm import ChatModel, ModelUnavailableError, Usage

FORMAT_VERSION = 1


class MissingAnswerError(RuntimeError):
    """The cache has no recorded answer for this prompt; it must be re-recorded."""


def prompt_key(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\x00{user}".encode()).hexdigest()


class AnswerCache:
    def __init__(
        self,
        model: str,
        answers: dict[str, str] | None = None,
        *,
        prompt_version: str = "",
    ) -> None:
        self.model = model
        self.prompt_version = prompt_version
        self.answers: dict[str, str] = answers or {}

    def __len__(self) -> int:
        return len(self.answers)

    @classmethod
    def load(cls, path: Path) -> AnswerCache:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("format") != FORMAT_VERSION:
            raise ValueError(f"{path.name}: unsupported format {raw.get('format')!r}")
        return cls(
            raw["model"], raw["answers"], prompt_version=raw.get("prompt_version", "")
        )

    def save(self, path: Path) -> None:
        payload = {
            "format": FORMAT_VERSION,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "answers": dict(sorted(self.answers.items())),
        }
        path.write_text(json.dumps(payload, indent=0) + "\n", encoding="utf-8")


class RecordingChatModel:
    """Wraps a live model and remembers every answer it gives."""

    def __init__(self, inner: ChatModel, cache: AnswerCache) -> None:
        self._inner = inner
        self.cache = cache

    @property
    def model(self) -> str:
        return self._inner.model

    def complete(self, system: str, user: str, usage: Usage | None = None) -> str:
        answer = self._inner.complete(system, user)
        self.cache.answers[prompt_key(system, user)] = answer
        return answer

    def stream(
        self, system: str, user: str, usage: Usage | None = None
    ) -> Iterator[str]:
        parts: list[str] = []
        for piece in self._inner.stream(system, user):
            parts.append(piece)
            yield piece
        self.cache.answers[prompt_key(system, user)] = "".join(parts)


class ReplayChatModel:
    """Serves recorded answers; never calls a model."""

    def __init__(self, cache: AnswerCache) -> None:
        self._cache = cache

    @property
    def model(self) -> str:
        return self._cache.model

    def _lookup(self, system: str, user: str) -> str:
        try:
            return self._cache.answers[prompt_key(system, user)]
        except KeyError:
            raise MissingAnswerError(
                "no recorded answer for this prompt: the retrieval, the context, a "
                "prompt or the golden set changed. Re-record with a live model: "
                "python -m leyline.evals.answers --record-answers "
                "evals/answers.azure.json (see docs/EVALUATION.md)"
            ) from None

    def complete(self, system: str, user: str, usage: Usage | None = None) -> str:
        answer = self._lookup(system, user)
        if not answer.strip():
            raise ModelUnavailableError("empty_response")
        return answer

    def stream(
        self, system: str, user: str, usage: Usage | None = None
    ) -> Iterator[str]:
        yield from re.findall(r"\S+\s*", self._lookup(system, user))
