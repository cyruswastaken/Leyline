from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
CORPUS_DIR = BASE_DIR / "corpus"


def parse_document(file_path: Path) -> dict:
    """
    Parse a document and extract its metadata and content.
    """

    text = file_path.read_text(encoding="utf-8")

    lines = text.splitlines()

    metadata = {}
    content_lines = []

    for line in lines:

        if line.startswith("Document Title:"):
            metadata["title"] = line.replace(
                "Document Title:", ""
            ).strip()

        elif line.startswith("Document ID:"):
            metadata["id"] = line.replace(
                "Document ID:", ""
            ).strip()

        elif line.startswith("Access Level:"):
            metadata["access_level"] = line.replace(
                "Access Level:", ""
            ).strip()

        else:
            content_lines.append(line)

    metadata["content"] = "\n".join(
        content_lines
    ).strip()

    return metadata


def load_documents() -> list:
    """
    Load all .txt documents from the corpus.
    """

    documents = []

    for file_path in CORPUS_DIR.rglob("*.txt"):

        document = parse_document(file_path)

        documents.append(document)

    return documents