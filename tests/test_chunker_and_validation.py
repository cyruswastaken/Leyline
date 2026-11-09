import itertools
import random
import re

import pytest

from leyline.adapters.search_postgres import to_or_query
from leyline.ingestion.chunker import chunk_sections
from leyline.ingestion.parsers import InvalidDocumentError, Section
from leyline.ingestion.validation import (
    MAX_GROUPS,
    MAX_TITLE_LENGTH,
    safe_filename,
    validate_groups,
    validate_title,
)


def words(chunk_text: str) -> int:
    return len(chunk_text.split())


def numbered_lines(count: int, per_line: int = 6) -> str:
    return "\n".join(
        f"{i}. " + " ".join(f"w{i}x{j}" for j in range(per_line)) for i in range(count)
    )


# --- chunker ----------------------------------------------------------------


def test_a_small_section_is_one_chunk_with_its_heading():
    chunks = chunk_sections(
        [Section("VPN > Setup", "Open the client.\nSign in.")],
        target_words=50,
        overlap_words=10,
    )
    assert len(chunks) == 1
    assert chunks[0].heading == "VPN > Setup"
    assert chunks[0].content == "Open the client.\nSign in."
    assert chunks[0].index == 0


def test_no_sections_means_no_chunks():
    assert chunk_sections([], target_words=50, overlap_words=10) == []


def test_chunks_never_exceed_the_target_size():
    chunks = chunk_sections(
        [Section("H", numbered_lines(60))], target_words=40, overlap_words=10
    )
    assert len(chunks) > 3
    assert all(words(c.content) <= 40 for c in chunks)


def test_chunks_keep_numbered_steps_whole_and_overlap_at_boundaries():
    text = numbered_lines(30)
    chunks = chunk_sections([Section(None, text)], target_words=40, overlap_words=14)
    lines_per_chunk = [c.content.splitlines() for c in chunks]
    for line in text.splitlines():  # nothing is split mid-line
        assert any(line in lines for lines in lines_per_chunk)
    for before, after in itertools.pairwise(lines_per_chunk):
        # the tail of one chunk is repeated at the start of the next
        assert after[0] in before and before[-1] in after


def test_chunks_never_cross_section_boundaries():
    sections = [
        Section("A", numbered_lines(20)),
        Section("B", numbered_lines(20).replace("w", "z")),
    ]
    chunks = chunk_sections(sections, target_words=40, overlap_words=10)
    for chunk in chunks:
        assert ("w" in chunk.content) != ("z" in chunk.content)
        assert (chunk.heading == "A") == ("w" in chunk.content)


def test_indices_are_contiguous_across_sections():
    chunks = chunk_sections(
        [Section("A", numbered_lines(15)), Section("B", numbered_lines(15))],
        target_words=30,
        overlap_words=5,
    )
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_a_very_long_single_line_is_split_by_words():
    chunks = chunk_sections(
        [Section(None, " ".join(f"w{i}" for i in range(500)))],
        target_words=100,
        overlap_words=20,
    )
    assert len(chunks) == 5
    assert all(words(c.content) <= 100 for c in chunks)
    assert "w0" in chunks[0].content and "w499" in chunks[-1].content


def test_chunking_is_deterministic():
    sections = [Section("A", numbered_lines(40))]
    first = chunk_sections(sections, target_words=35, overlap_words=9)
    second = chunk_sections(sections, target_words=35, overlap_words=9)
    assert first == second


def test_overlap_must_be_smaller_than_the_target():
    with pytest.raises(ValueError, match="overlap"):
        chunk_sections([Section(None, "x")], target_words=10, overlap_words=10)


@pytest.mark.parametrize("seed", range(40))
def test_random_documents_always_chunk_correctly_and_terminate(seed):
    """Property check over many random shapes: coverage, bounds, termination."""
    rng = random.Random(seed)
    target = rng.randint(1, 60)
    overlap = rng.randint(0, target - 1)
    sections = []
    for s in range(rng.randint(1, 4)):
        lines = [
            " ".join(f"s{s}l{n}w{k}" for k in range(rng.randint(1, 90)))
            for n in range(rng.randint(1, 25))
        ]
        sections.append(Section(f"H{s}", "\n".join(lines)))

    chunks = chunk_sections(sections, target_words=target, overlap_words=overlap)

    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(1 <= words(c.content) <= target for c in chunks)
    for s, section in enumerate(sections):
        covered = {
            token for c in chunks if c.heading == f"H{s}" for token in c.content.split()
        }
        assert set(section.text.split()) <= covered, "every word must be indexed"


# --- validation -------------------------------------------------------------


def test_titles_are_trimmed_and_control_characters_removed():
    assert validate_title("  VPN\tSetup\n Guide  ") == "VPN Setup  Guide"


@pytest.mark.parametrize("title", ["", "   ", "\n\t"])
def test_blank_titles_are_rejected(title):
    with pytest.raises(InvalidDocumentError, match="title is required"):
        validate_title(title)


def test_overlong_titles_are_rejected():
    assert validate_title("a" * MAX_TITLE_LENGTH)
    with pytest.raises(InvalidDocumentError, match="longer than"):
        validate_title("a" * (MAX_TITLE_LENGTH + 1))


def test_valid_groups_are_normalised():
    assert validate_groups([" staff ", "hr", "staff", ""]) == {"staff", "hr"}


def test_entra_object_ids_and_the_everyone_entry_are_valid_groups():
    guid = "0b1f4a52-7c1e-4a9e-9d6d-8f3b2c1a5e77"
    assert validate_groups([guid, "*"]) == {guid, "*"}


def test_an_empty_acl_is_valid_and_means_nobody():
    assert validate_groups([]) == frozenset()


@pytest.mark.parametrize(
    "group",
    ["a;b", "<script>", "x" * 129, "line\nbreak", "quote'", "back`tick", "sec*"],
)
def test_malformed_group_names_are_rejected(group):
    with pytest.raises(InvalidDocumentError, match="Invalid group"):
        validate_groups([group])


def test_too_many_groups_are_rejected():
    assert len(validate_groups([f"g{i}" for i in range(MAX_GROUPS)])) == MAX_GROUPS
    with pytest.raises(InvalidDocumentError, match="At most"):
        validate_groups([f"g{i}" for i in range(MAX_GROUPS + 1)])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\evil.docx", "evil.docx"),
        ("na\x00me\x1f.txt", "name.txt"),
        ("", "upload"),
        ("/", "upload"),
        ("a" * 400 + ".md", ("a" * 400 + ".md")[:255]),
    ],
)
def test_filenames_become_safe_display_names(raw, expected):
    assert safe_filename(raw) == expected


# --- search query sanitising ------------------------------------------------


def test_free_text_becomes_an_or_query():
    assert (
        to_or_query("How do I set up the VPN?") == "how | do | i | set | up | the | vpn"
    )


def test_query_operators_cannot_be_injected():
    query = to_or_query("a & b | !c <-> d:* ) ( ' ; DROP TABLE chunks; --")
    assert query is not None
    # only plain words joined by the OR operator survive
    assert re.fullmatch(r"[a-z0-9]+( \| [a-z0-9]+)*", query)


def test_duplicate_terms_collapse_and_the_term_count_is_bounded():
    assert to_or_query("vpn VPN vpn") == "vpn"
    many = to_or_query(" ".join(f"term{i}" for i in range(100)))
    assert many is not None and len(many.split(" | ")) == 32


@pytest.mark.parametrize("text", ["", "   ", "?!@#$%", "日本語", "--; ()"])
def test_text_with_nothing_searchable_yields_no_query(text):
    assert to_or_query(text) is None
