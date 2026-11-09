from pathlib import Path
from typing import Any

from leyline.config import get_settings

# Number of words in each chunk.
CHUNK_SIZE = 150

# Number of words repeated between adjacent chunks.
CHUNK_OVERLAP = 30


def parse_document(file_path: Path) -> dict[str, Any]:
    """
    Parse a document and extract its metadata and content.
    """

    text = file_path.read_text(encoding="utf-8")

    lines = text.splitlines()

    metadata: dict[str, Any] = {}
    content_lines = []

    header_fields = {
        "Document Title:": "title",
        "Document ID:": "id",
        "Access Level:": "access_level",
    }

    # Metadata is only honoured in the leading header block. Once body
    # text begins, a line such as "Access Level: general" is plain
    # content and can never change the document's classification.
    in_header = True

    for line in lines:
        if in_header:
            if not line.strip():
                continue

            for prefix, key in header_fields.items():
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


def chunk_document(
    document: dict[str, Any], chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[dict[str, Any]]:
    """
    Split a document into overlapping word-based chunks.

    Metadata from the original document is preserved
    on every chunk.
    """

    content = document.get("content", "").strip()

    if not content:
        return []

    words = content.split()

    chunks = []

    start = 0
    chunk_number = 0

    while start < len(words):
        end = start + chunk_size

        chunk_words = words[start:end]

        chunk_content = " ".join(chunk_words).strip()

        if chunk_content:
            chunks.append(
                {
                    **document,
                    "chunk_id": (
                        f"{document.get('id', 'unknown')}-chunk-{chunk_number}"
                    ),
                    "chunk_index": chunk_number,
                    "content": chunk_content,
                }
            )

        chunk_number += 1

        # Stop once we have reached
        # the end of the document.
        if end >= len(words):
            break

        start = end - overlap

    return chunks


def load_documents() -> list[dict[str, Any]]:
    """
    Load all .txt documents from the corpus.
    """

    documents = []

    for file_path in get_settings().corpus_dir.rglob("*.txt"):
        document = parse_document(file_path)

        documents.append(document)

    return documents
