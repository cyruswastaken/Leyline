"""Turn uploaded files into text sections.

Files are validated by their *content*, never by the filename or the
browser-supplied content type: a ``.pdf`` that is not a PDF is rejected, and
the stored content type is derived from what the bytes actually are.

Everything here treats the input as hostile: bounded size, bounded page
count, zip-bomb checks, no external entity resolution.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal

from defusedxml import ElementTree as SafeET  # type: ignore[import-untyped]
from pypdf import PdfReader

Kind = Literal["txt", "md", "pdf", "docx"]

CONTENT_TYPES: dict[Kind, str] = {
    "txt": "text/plain",
    "md": "text/markdown",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_EXTENSIONS: dict[str, Kind] = {f".{kind}": kind for kind in CONTENT_TYPES}
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING_STYLE = re.compile(r"^Heading([1-9])$", re.IGNORECASE)
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_FENCE = re.compile(r"^\s*(```|~~~)")


class InvalidDocumentError(ValueError):
    """The file is unusable. Permanent: retrying will not help.

    The message is written for administrators and is safe to display.
    """


@dataclass(frozen=True)
class Section:
    """A run of text under a heading path such as ``"VPN > Troubleshooting"``."""

    heading: str | None
    text: str


@dataclass(frozen=True)
class Limits:
    max_bytes: int
    max_pdf_pages: int
    max_docx_uncompressed_bytes: int


def detect_kind(filename: str, data: bytes, limits: Limits) -> Kind:
    """Validate an upload and return its kind, or raise InvalidDocumentError."""
    if not data:
        raise InvalidDocumentError("The file is empty.")
    if len(data) > limits.max_bytes:
        raise InvalidDocumentError(
            f"The file is larger than the {limits.max_bytes // (1024 * 1024)} MB limit."
        )

    suffix = PurePath(filename.replace("\\", "/")).suffix.lower()
    kind = _EXTENSIONS.get(suffix)
    if kind is None:
        allowed = ", ".join(sorted(_EXTENSIONS))
        raise InvalidDocumentError(f"Unsupported file type. Allowed: {allowed}.")

    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise InvalidDocumentError("The file is not a valid PDF.")
    elif kind == "docx":
        _check_docx_container(data, limits)
    else:
        _decode_text(data)
    return kind


def parse(kind: Kind, data: bytes, limits: Limits) -> list[Section]:
    """Extract sections. Raises InvalidDocumentError for unusable content."""
    if kind == "txt":
        sections = [Section(None, _decode_text(data))]
    elif kind == "md":
        sections = _parse_markdown(_decode_text(data))
    elif kind == "pdf":
        sections = _parse_pdf(data, limits)
    else:
        sections = _parse_docx(data, limits)

    sections = [s for s in sections if s.text.strip()]
    if not sections:
        raise InvalidDocumentError("The document contains no extractable text.")
    return sections


# -- text and markdown -------------------------------------------------------


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise InvalidDocumentError("The file is binary, not text.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidDocumentError("The text file is not valid UTF-8.") from None


def _parse_markdown(text: str) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if body:
            heading = " > ".join(title for _, title in stack) or None
            sections.append(Section(heading, body))

    for line in text.splitlines():
        if _MD_FENCE.match(line):
            in_fence = not in_fence
            buffer.append(line)
            continue
        match = None if in_fence else _MD_HEADING.match(line)
        if match is None:
            buffer.append(line)
            continue
        flush()
        level = len(match.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, match.group(2).strip()))
    flush()
    return sections


# -- pdf ---------------------------------------------------------------------


def _parse_pdf(data: bytes, limits: Limits) -> list[Section]:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise InvalidDocumentError("Encrypted PDFs are not supported.")
        if len(reader.pages) > limits.max_pdf_pages:
            raise InvalidDocumentError(
                f"The PDF has more than {limits.max_pdf_pages} pages."
            )
        sections = [
            Section(f"Page {number}", (page.extract_text() or "").strip())
            for number, page in enumerate(reader.pages, start=1)
        ]
    except InvalidDocumentError:
        raise
    except Exception:
        raise InvalidDocumentError("The PDF could not be read.") from None

    if not any(section.text for section in sections):
        raise InvalidDocumentError(
            "The PDF has no extractable text (scanned images are not supported)."
        )
    return sections


# -- docx --------------------------------------------------------------------


def _check_docx_container(data: bytes, limits: Limits) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InvalidDocumentError("The file is not a valid DOCX.") from None
    with archive:
        names = set(archive.namelist())
        if "word/document.xml" not in names or "[Content_Types].xml" not in names:
            raise InvalidDocumentError("The file is not a valid DOCX.")
        total = sum(info.file_size for info in archive.infolist())
        if total > limits.max_docx_uncompressed_bytes:
            raise InvalidDocumentError("The DOCX expands to an unreasonable size.")


def _parse_docx(data: bytes, limits: Limits) -> list[Section]:
    _check_docx_container(data, limits)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = SafeET.fromstring(archive.read("word/document.xml"), forbid_dtd=True)
    except Exception:
        raise InvalidDocumentError("The DOCX could not be read.") from None

    body = root.find(f"{_W_NS}body")
    if body is None:
        raise InvalidDocumentError("The DOCX has no body.")

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        buffer.clear()
        if text:
            sections.append(
                Section(" > ".join(title for _, title in stack) or None, text)
            )

    for element in body:
        if element.tag == f"{_W_NS}p":
            text = _paragraph_text(element)
            level = _heading_level(element)
            if level is not None and text:
                flush()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, text))
            elif text:
                buffer.append(text)
        elif element.tag == f"{_W_NS}tbl":
            for row in element.iter(f"{_W_NS}tr"):
                cells = [
                    " ".join(_paragraph_text(p) for p in cell.iter(f"{_W_NS}p")).strip()
                    for cell in row.findall(f"{_W_NS}tc")
                ]
                line = " | ".join(cell for cell in cells if cell)
                if line:
                    buffer.append(line)
    flush()
    return sections


def _paragraph_text(paragraph: object) -> str:
    parts: list[str] = []
    for node in paragraph.iter():  # type: ignore[attr-defined]
        if node.tag == f"{_W_NS}t" and node.text:
            parts.append(node.text)
        elif node.tag in (f"{_W_NS}tab", f"{_W_NS}br"):
            parts.append(" ")
    return "".join(parts).strip()


def _heading_level(paragraph: object) -> int | None:
    style = paragraph.find(f"{_W_NS}pPr/{_W_NS}pStyle")  # type: ignore[attr-defined]
    if style is None:
        return None
    value = style.get(f"{_W_NS}val") or ""
    if value.lower() == "title":
        return 0  # above Heading 1, so headings nest beneath the title
    match = _HEADING_STYLE.match(value)
    return int(match.group(1)) if match else None
