from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
CORPUS_DIR = BASE_DIR / "corpus"


# Number of words in each chunk.
CHUNK_SIZE = 150

# Number of words repeated between adjacent chunks.
CHUNK_OVERLAP = 30


def parse_document(file_path: Path) -> dict:
    """
    Parse a document and extract its metadata and content.
    """

    text = file_path.read_text(
        encoding="utf-8"
    )

    lines = text.splitlines()

    metadata = {}
    content_lines = []

    for line in lines:

        if line.startswith("Document Title:"):

            metadata["title"] = line.replace(
                "Document Title:",
                ""
            ).strip()

        elif line.startswith("Document ID:"):

            metadata["id"] = line.replace(
                "Document ID:",
                ""
            ).strip()

        elif line.startswith("Access Level:"):

            metadata["access_level"] = line.replace(
                "Access Level:",
                ""
            ).strip()

        else:

            content_lines.append(line)

    metadata["content"] = "\n".join(
        content_lines
    ).strip()

    return metadata


def chunk_document(
    document: dict,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP
) -> list:
    """
    Split a document into overlapping word-based chunks.

    Metadata from the original document is preserved
    on every chunk.
    """

    content = document.get(
        "content",
        ""
    ).strip()

    if not content:
        return []

    words = content.split()

    chunks = []

    start = 0
    chunk_number = 0

    while start < len(words):

        end = start + chunk_size

        chunk_words = words[
            start:end
        ]

        chunk_content = " ".join(
            chunk_words
        ).strip()

        if chunk_content:

            chunks.append({
                **document,

                "chunk_id": (
                    f"{document.get('id', 'unknown')}"
                    f"-chunk-{chunk_number}"
                ),

                "chunk_index": chunk_number,

                "content": chunk_content
            })

        chunk_number += 1

        # Stop once we have reached
        # the end of the document.
        if end >= len(words):
            break

        start = end - overlap

    return chunks


def load_documents() -> list:
    """
    Load all .txt documents from the corpus.
    """

    documents = []

    for file_path in CORPUS_DIR.rglob(
        "*.txt"
    ):

        document = parse_document(
            file_path
        )

        documents.append(
            document
        )

    return documents