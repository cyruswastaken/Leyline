"""Heading-aware, line-preserving chunker.

Chunks never cross a section boundary, so each one carries an accurate
heading for citations. Within a section, whole lines (list items, numbered
steps) are kept together and chunks overlap by roughly ``overlap_words`` so
context at the boundary is not lost. Deterministic: the same input always
yields the same chunks, which makes re-indexing idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from leyline.ingestion.parsers import Section


@dataclass(frozen=True)
class ChunkDraft:
    index: int
    heading: str | None
    content: str


def _units(text: str, target_words: int) -> list[str]:
    """Non-empty lines; a line longer than ``target_words`` is split by words."""
    units: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        words = line.split()
        if len(words) <= target_words:
            units.append(line)
        else:
            units.extend(
                " ".join(words[i : i + target_words])
                for i in range(0, len(words), target_words)
            )
    return units


def _word_count(unit: str) -> int:
    return len(unit.split())


def chunk_sections(
    sections: Sequence[Section], *, target_words: int, overlap_words: int
) -> list[ChunkDraft]:
    if overlap_words >= target_words:
        raise ValueError("overlap must be smaller than the target size")

    drafts: list[ChunkDraft] = []

    for section in sections:
        units = _units(section.text, target_words)
        current: list[str] = []
        current_words = 0
        fresh = 0  # units added since the last flush (excludes carried overlap)

        def flush(section: Section = section, current: list[str] = current) -> None:
            drafts.append(ChunkDraft(len(drafts), section.heading, "\n".join(current)))

        for unit in units:
            words = _word_count(unit)
            if current and fresh and current_words + words > target_words:
                flush()
                # Carry the tail of this chunk into the next as overlap.
                tail: list[str] = []
                carried = 0
                for previous in reversed(current):
                    size = _word_count(previous)
                    if carried + size > overlap_words:
                        break
                    tail.insert(0, previous)
                    carried += size
                # The overlap must leave room for the next unit, or the new
                # chunk would exceed the target size: shrink it from the front.
                while tail and carried + words > target_words:
                    carried -= _word_count(tail.pop(0))
                current[:] = tail
                current_words = carried
                fresh = 0
            current.append(unit)
            current_words += words
            fresh += 1

        if current and fresh:
            flush()

    return drafts
