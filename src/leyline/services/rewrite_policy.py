"""Decide, without a model call, whether a follow-up needs rewriting.

A rewrite costs a whole model round trip (about a second) before retrieval can
start, so it should only run when the question cannot be searched as written.
The check is deliberately conservative: when in doubt the question is rewritten,
which is only slower, never wrong. It looks at the question text alone (never at
earlier answers or documents), so it cannot widen what anyone may read.
"""

from __future__ import annotations

import re

# A question this short rarely says what it is about.
_MIN_STANDALONE_WORDS = 4

# Words that point back at something said earlier. "that" and "this" are here even
# though they are sometimes harmless ("this laptop"): the cost of a false alarm is
# one extra model call.
_REFERENCES = frozenset(
    {
        "it",
        "its",
        "it's",
        "that",
        "this",
        "those",
        "these",
        "them",
        "they",
        "their",
        "he",
        "she",
        "one",
        "ones",
        "same",
        "other",
        "another",
        "also",
        "too",
        "again",
        "instead",
        "else",
        "previous",
        "above",
        "earlier",
        "former",
        "latter",
        "such",
        "then",
    }
)

# Openers that continue the last question rather than start a new one.
_CONTINUATIONS = re.compile(
    r"^\s*(and|but|or|also|then|so|what about|how about|what if|why|how come|"
    r"does that|is that|can i also|can you also)\b",
    re.IGNORECASE,
)

_WORD = re.compile(r"[a-z0-9']+")


def needs_rewrite(message: str) -> bool:
    """True when ``message`` may depend on earlier questions to be understood."""
    words = _WORD.findall(message.lower())
    if len(words) < _MIN_STANDALONE_WORDS:
        return True
    if _CONTINUATIONS.match(message):
        return True
    return any(word in _REFERENCES for word in words)
