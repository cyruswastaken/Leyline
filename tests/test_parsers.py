import io
import zipfile

import pytest

from leyline.ingestion.parsers import (
    CONTENT_TYPES,
    InvalidDocumentError,
    Limits,
    Section,
    detect_kind,
    parse,
)
from tests.builders import (
    make_blank_pdf,
    make_docx,
    make_encrypted_pdf,
    make_pdf,
)

LIMITS = Limits(
    max_bytes=1024 * 1024, max_pdf_pages=3, max_docx_uncompressed_bytes=512 * 1024
)


def kind_of(filename, data):
    return detect_kind(filename, data, LIMITS)


# --- upload validation ------------------------------------------------------


def test_each_supported_type_is_recognised_by_content():
    assert kind_of("a.txt", b"hello") == "txt"
    assert kind_of("a.md", b"# Title\ntext") == "md"
    assert kind_of("a.pdf", make_pdf(["hi"])) == "pdf"
    assert kind_of("a.docx", make_docx([("hi", None)])) == "docx"


def test_content_types_are_derived_from_the_detected_kind():
    assert CONTENT_TYPES["pdf"] == "application/pdf"
    assert CONTENT_TYPES["md"] == "text/markdown"
    assert CONTENT_TYPES["txt"] == "text/plain"
    assert CONTENT_TYPES["docx"].startswith("application/vnd.openxmlformats")


def test_extension_matching_is_case_insensitive():
    assert kind_of("REPORT.PDF", make_pdf(["hi"])) == "pdf"


@pytest.mark.parametrize(
    "filename",
    [
        "malware.exe",
        "page.html",
        "archive.zip",
        "noextension",
        "x.pdf.exe",
        "x.",
        ".md.js",
    ],
)
def test_unsupported_extensions_are_rejected(filename):
    with pytest.raises(InvalidDocumentError, match="Unsupported file type"):
        kind_of(filename, b"hello")


def test_a_path_in_the_filename_does_not_confuse_type_detection():
    assert kind_of("..\\..\\evil\\notes.txt", b"hello") == "txt"
    assert kind_of("../../etc/passwd.md", b"text") == "md"
    with pytest.raises(InvalidDocumentError):
        kind_of("../../etc/passwd", b"text")


def test_empty_files_are_rejected():
    with pytest.raises(InvalidDocumentError, match="empty"):
        kind_of("a.txt", b"")


def test_oversized_files_are_rejected():
    with pytest.raises(InvalidDocumentError, match="larger than"):
        kind_of("a.txt", b"x" * (LIMITS.max_bytes + 1))


def test_a_file_pretending_to_be_a_pdf_is_rejected():
    with pytest.raises(InvalidDocumentError, match="not a valid PDF"):
        kind_of("a.pdf", b"MZ\x90\x00 this is an executable")


def test_a_file_pretending_to_be_a_docx_is_rejected():
    with pytest.raises(InvalidDocumentError, match="not a valid DOCX"):
        kind_of("a.docx", b"plain text, not a zip")


def test_a_zip_that_is_not_a_docx_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "hi")
    with pytest.raises(InvalidDocumentError, match="not a valid DOCX"):
        kind_of("a.docx", buffer.getvalue())


def test_binary_data_named_txt_is_rejected():
    with pytest.raises(InvalidDocumentError, match="binary"):
        kind_of("a.txt", b"abc\x00def")


def test_text_that_is_not_utf8_is_rejected():
    with pytest.raises(InvalidDocumentError, match="UTF-8"):
        kind_of("a.txt", "caf\xe9".encode("latin-1"))


def test_utf8_with_bom_is_accepted():
    data = "﻿hello".encode()
    assert kind_of("a.txt", data) == "txt"
    assert parse("txt", data, LIMITS)[0].text == "hello"


# --- text and markdown ------------------------------------------------------


def test_plain_text_is_one_section_without_a_heading():
    assert parse("txt", b"line one\nline two", LIMITS) == [
        Section(None, "line one\nline two")
    ]


def test_blank_documents_are_rejected():
    with pytest.raises(InvalidDocumentError, match="no extractable text"):
        parse("txt", b"   \n\n  ", LIMITS)


def test_markdown_headings_build_a_path():
    md = (
        b"Intro text\n\n# VPN\nOverview\n\n## Setup\nStep one\n\n"
        b"## Troubleshooting\nReboot\n\n# Passwords\nRotate"
    )
    assert parse("md", md, LIMITS) == [
        Section(None, "Intro text"),
        Section("VPN", "Overview"),
        Section("VPN > Setup", "Step one"),
        Section("VPN > Troubleshooting", "Reboot"),
        Section("Passwords", "Rotate"),
    ]


def test_a_hash_inside_a_code_fence_is_not_a_heading():
    md = b"# Real\n```\n# not a heading\nls\n```\nafter"
    sections = parse("md", md, LIMITS)
    assert [s.heading for s in sections] == ["Real"]
    assert "# not a heading" in sections[0].text


def test_markdown_headings_with_closing_hashes_and_deeper_levels():
    sections = parse("md", b"# A #\ntext\n### C\ndeep", LIMITS)
    assert [s.heading for s in sections] == ["A", "A > C"]


def test_empty_sections_are_dropped():
    sections = parse("md", b"# Empty\n\n# Full\ncontent", LIMITS)
    assert [s.heading for s in sections] == ["Full"]


# --- pdf ----------------------------------------------------------------------


def test_pdf_text_is_extracted_per_page():
    sections = parse("pdf", make_pdf(["First page text", "Second page text"]), LIMITS)
    assert [s.heading for s in sections] == ["Page 1", "Page 2"]
    assert "First page text" in sections[0].text
    assert "Second page text" in sections[1].text


def test_pdf_with_too_many_pages_is_rejected():
    with pytest.raises(InvalidDocumentError, match="more than 3 pages"):
        parse("pdf", make_pdf(["a", "b", "c", "d"]), LIMITS)


def test_encrypted_pdf_is_rejected():
    with pytest.raises(InvalidDocumentError, match="Encrypted"):
        parse("pdf", make_encrypted_pdf(), LIMITS)


def test_pdf_without_text_is_rejected_with_a_helpful_message():
    with pytest.raises(InvalidDocumentError, match="scanned"):
        parse("pdf", make_blank_pdf(), LIMITS)


def test_a_corrupt_pdf_is_rejected_without_leaking_internals():
    with pytest.raises(InvalidDocumentError, match="could not be read") as info:
        parse("pdf", b"%PDF-1.4\nthis is not a real pdf body", LIMITS)
    assert "Traceback" not in str(info.value)


# --- docx ---------------------------------------------------------------------


def test_docx_headings_paragraphs_and_tables():
    data = make_docx(
        [
            ("Employee Handbook", "Title"),
            ("Intro paragraph.", None),
            ("Leave", "Heading1"),
            ("Annual leave is 25 days.", None),
            ("Sick leave", "Heading2"),
            ("Call your manager.", None),
            [["Type", "Days"], ["Annual", "25"]],
        ]
    )
    sections = parse("docx", data, LIMITS)
    assert sections[0] == Section("Employee Handbook", "Intro paragraph.")
    assert sections[1] == Section(
        "Employee Handbook > Leave", "Annual leave is 25 days."
    )
    assert sections[2].heading == "Employee Handbook > Leave > Sick leave"
    assert "Type | Days" in sections[2].text
    assert "Annual | 25" in sections[2].text


def test_docx_without_text_is_rejected():
    with pytest.raises(InvalidDocumentError, match="no extractable text"):
        parse("docx", make_docx([]), LIMITS)


def test_docx_xml_entity_expansion_is_blocked():
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>"
    )
    with pytest.raises(InvalidDocumentError, match="could not be read"):
        parse("docx", make_docx([], document_xml=bomb), LIMITS)


def test_docx_external_entities_are_blocked():
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
    )
    with pytest.raises(InvalidDocumentError):
        parse("docx", make_docx([], document_xml=xxe), LIMITS)


def test_a_docx_that_expands_enormously_is_rejected_before_parsing():
    huge = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>"
        + "a" * (LIMITS.max_docx_uncompressed_bytes + 1)
        + "</w:t></w:r></w:p></w:body></w:document>"
    )
    data = make_docx([], document_xml=huge)
    assert len(data) < LIMITS.max_bytes  # tiny on the wire, huge when unpacked
    with pytest.raises(InvalidDocumentError, match="unreasonable size"):
        kind_of("bomb.docx", data)


def test_docx_with_broken_xml_is_rejected():
    with pytest.raises(InvalidDocumentError, match="could not be read"):
        parse("docx", make_docx([], document_xml="<w:document><unclosed>"), LIMITS)
