"""Defences around the model that do not depend on the model behaving.

The real security boundary is elsewhere (authorization before retrieval, a model
with no tools). These are the extra layers:

* ``assess_message``   scores prompt-injection signals in the user's message; it
                       logs, and blocks only blatant attacks. Phrase lists are
                       easy to evade, so they are never the main defence.
* ``build_context``    delimits retrieved text as untrusted data, neutralises
                       attempts to close the delimiter, and fits a token budget.
* ``verify_citations`` keeps only citation markers that point at sources that
                       were actually retrieved for this answer.
* ``StreamRedactor``   removes credential-shaped strings from model output, also
                       when they arrive split across stream chunks.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from leyline.ports.search import SearchHit

# --- input scoring ------------------------------------------------------------

_SIGNALS: tuple[tuple[str, int, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        2,
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|earlier|all|your)\b"
            r".{0,20}\b(instructions?|rules?|prompts?|guidelines?)\b",
            re.I | re.S,
        ),
    ),
    (
        "reveal_system_prompt",
        2,
        re.compile(
            r"\b(reveal|show|print|repeat|display|leak|output)\b.{0,40}"
            r"\b(system\s+prompt|your\s+instructions|hidden\s+instructions|initial\s+prompt)\b",
            re.I | re.S,
        ),
    ),
    (
        "enumerate_documents",
        1,
        re.compile(
            r"\b(list|show|dump|enumerate)\b.{0,20}\ball\b.{0,20}\b(documents?|files?|sources?)\b",
            re.I | re.S,
        ),
    ),
    (
        "role_override",
        1,
        re.compile(
            r"\b(you are now|act as|pretend (to be|you are)|developer mode"
            r"|jailbreak)\b",
            re.I,
        ),
    ),
    (
        "credential_request",
        1,
        re.compile(
            r"\b(password|passwd|api[ _-]?key|secret|token|private key)s?\b"
            r".{0,30}\b(of|for|from)\b.{0,30}"
            r"\b(admin|root|service|database|ceo|everyone)\b",
            re.I | re.S,
        ),
    ),
)

# A single weak signal is only logged. Two strong ones together (e.g. override
# + reveal) are an unmistakable attack and are refused.
DEFAULT_BLOCK_SCORE = 4


@dataclass(frozen=True)
class Assessment:
    score: int
    signals: tuple[str, ...]

    def blocked(self, threshold: int = DEFAULT_BLOCK_SCORE) -> bool:
        return self.score >= threshold


def assess_message(message: str) -> Assessment:
    signals = [name for name, _, pattern in _SIGNALS if pattern.search(message)]
    score = sum(weight for name, weight, _ in _SIGNALS if name in signals)
    return Assessment(score=score, signals=tuple(signals))


# --- untrusted context --------------------------------------------------------

_TAG_BREAKOUT = re.compile(r"<\s*/?\s*(source|untrusted_context)\b[^>]*>", re.I)


def _neutralise(text: str) -> str:
    """Remove anything that could close or forge the delimiters we add."""
    return _TAG_BREAKOUT.sub("[removed tag]", text)


def estimate_tokens(text: str) -> int:
    # Conservative rule of thumb (~4 characters per token) without a tokenizer.
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class BuiltContext:
    text: str
    used: tuple[SearchHit, ...]  # hits that fit the budget, in citation order


def build_context(hits: Sequence[SearchHit], *, token_budget: int) -> BuiltContext:
    """Numbered, delimited sources. Hits that do not fit the budget are dropped
    (lowest ranked first); the first hit is always kept, truncated if needed."""
    parts: list[str] = []
    used: list[SearchHit] = []
    remaining = token_budget
    for hit in hits:
        number = len(used) + 1
        title = _neutralise(hit.title).replace('"', "'")
        heading = _neutralise(hit.heading).replace('"', "'") if hit.heading else ""
        body = _neutralise(hit.content)
        header = f'<source id="{number}" title="{title}"' + (
            f' heading="{heading}">' if heading else ">"
        )
        cost = estimate_tokens(header + body) + 4
        if cost > remaining:
            if used:
                break
            body = body[: max(200, remaining * 4)]
        parts.append(f"{header}\n{body}\n</source>")
        used.append(hit)
        remaining -= cost
    return BuiltContext(
        text="<untrusted_context>\n" + "\n".join(parts) + "\n</untrusted_context>",
        used=tuple(used),
    )


# --- citations ----------------------------------------------------------------

_MARKER = re.compile(r"\[(\d{1,2})\]")


def verify_citations(answer: str, source_count: int) -> tuple[str, list[int]]:
    """Drop markers like ``[7]`` that do not point at a real source.

    Returns the cleaned answer and the (sorted, unique) source numbers it cites.
    """
    cited: set[int] = set()

    def keep(match: re.Match[str]) -> str:
        number = int(match.group(1))
        if 1 <= number <= source_count:
            cited.add(number)
            return match.group(0)
        return ""

    cleaned = _MARKER.sub(keep, answer)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return cleaned, sorted(cited)


# --- system prompt leakage ----------------------------------------------------------

LEAK_WINDOW = 8  # consecutive words copied from the prompt that count as a leak
PROMPT_LEAK_REPLY = (
    "I can't share my instructions, but I'm happy to help with an IT question."
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _shingles(prompt: str) -> frozenset[tuple[str, ...]]:
    words = _words(prompt)
    return frozenset(
        tuple(words[i : i + LEAK_WINDOW]) for i in range(len(words) - LEAK_WINDOW + 1)
    )


def leaks_prompt(answer: str, prompt: str) -> bool:
    """Does the answer reproduce a run of the system prompt word for word?"""
    shingles = _shingles(prompt)
    words = _words(answer)
    return any(
        tuple(words[i : i + LEAK_WINDOW]) in shingles
        for i in range(len(words) - LEAK_WINDOW + 1)
    )


# --- output redaction ---------------------------------------------------------

REDACTED = "[redacted]"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:sk|pk|ghp|gho|xox[bap])[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b(?:pass(?:word|wd)?|pwd|secret|api[_ -]?key|access[_ -]?token|token)"
        r"\s*(?:is|=|:)\s*[\"']?[^\s\"']{6,}"
    ),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
)


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class StreamRedactor:
    """Redacts a stream without letting a secret slip out in two halves.

    Text is held back until at least ``holdback`` characters follow it (and the
    cut falls on whitespace), so any pattern shorter than that is seen whole
    before anything of it is released.
    """

    def __init__(self, holdback: int = 96) -> None:
        self._buffer = ""
        self._holdback = holdback

    def feed(self, piece: str) -> str:
        buffer = self._buffer + piece
        # Only text followed by whitespace is complete. A trailing word may be
        # the first half of a secret, so it is left alone until it finishes.
        settled = max(buffer.rfind(" "), buffer.rfind("\n")) + 1
        self._buffer = redact(buffer[:settled]) + buffer[settled:]
        limit = len(self._buffer) - self._holdback
        if limit <= 0:
            return ""
        cut = max(self._buffer.rfind(" ", 0, limit), self._buffer.rfind("\n", 0, limit))
        if cut <= 0:
            return ""
        released, self._buffer = self._buffer[: cut + 1], self._buffer[cut + 1 :]
        return released

    def flush(self) -> str:
        released, self._buffer = redact(self._buffer), ""
        return released
