"""Parser for the legacy corpus header format.

Corpus files start with a header block (Document Title / Document ID /
Access Level / optional Groups). Metadata is honoured only in that leading
block: once body text begins, a line such as ``Access Level: general`` is
plain content and can never change a document's classification.

Used by ``scripts/seed_corpus.py`` to import the synthetic corpus through the
normal ingestion path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

HEADER_FIELDS = {
    "Document Title:": "title",
    "Document ID:": "id",
    "Access Level:": "access_level",
    "Groups:": "groups",
}


def parse_document(file_path: Path) -> dict[str, Any]:
    """Parse a document and extract its metadata and content."""
    lines = file_path.read_text(encoding="utf-8").splitlines()

    metadata: dict[str, Any] = {}
    content_lines: list[str] = []
    in_header = True

    for line in lines:
        if in_header:
            if not line.strip():
                continue
            for prefix, key in HEADER_FIELDS.items():
                if line.startswith(prefix):
                    # First occurrence wins.
                    metadata.setdefault(key, line[len(prefix) :].strip())
                    break
            else:
                in_header = False
                content_lines.append(line)
        else:
            content_lines.append(line)

    metadata["content"] = "\n".join(content_lines).strip()
    return metadata
