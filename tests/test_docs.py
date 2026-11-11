"""The operating documents must stay true: commands name real switches, links resolve, and the
decision records are complete and indexed. A runbook that tells someone to run a flag that does not
exist is worse than no runbook, so this is checked."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ADR = DOCS / "adr"
INFRA = ROOT / "infra"
RUNBOOKS = DOCS / "RUNBOOKS.md"

COMMON_PARAMETERS = {"WhatIf", "Verbose", "Confirm", "Debug"}
REQUIRED_SECTIONS = (
    "**Status:**",
    "## Context",
    "## Decision",
    "## Consequences",
    "## Alternatives considered",
    "## Evidence",
)


def script_parameters(script: Path) -> set[str]:
    text = script.read_text(encoding="utf-8")
    start = text.index("param(")
    end = text.index("\n)", start)
    return set(re.findall(r"\$([A-Za-z]+)", text[start:end])) | COMMON_PARAMETERS


def test_every_infra_script_the_runbooks_mention_exists_and_takes_those_switches():
    text = RUNBOOKS.read_text(encoding="utf-8")
    mentions = re.findall(r"\./infra/(\w+)\.ps1([^`\n|]*)", text)
    assert len(mentions) >= 15
    for name, rest in mentions:
        script = INFRA / f"{name}.ps1"
        assert script.is_file(), f"{name}.ps1 is mentioned but does not exist"
        known = script_parameters(script)
        for flag in re.findall(r"(?<![\w/.])-([A-Za-z]+)", rest):
            assert flag in known, f"RUNBOOKS: ./infra/{name}.ps1 has no -{flag}"


def test_the_switch_lists_in_the_quick_reference_are_real():
    text = RUNBOOKS.read_text(encoding="utf-8")
    table = text.split("| Script | Purpose |", 1)[1].split("\n\n", 1)[0]
    for line in table.splitlines():
        match = re.search(r"\./infra/(\w+)\.ps1", line)
        if not match:
            continue
        known = script_parameters(INFRA / f"{match.group(1)}.ps1")
        for flag in re.findall(r"`-([A-Za-z]+)`", line):
            assert flag in known, f"{match.group(1)}.ps1 has no -{flag}"


def test_relative_links_in_the_new_documents_resolve():
    for document in [RUNBOOKS, ADR / "README.md", *sorted(ADR.glob("0*.md"))]:
        for target in re.findall(
            r"\]\(([^)#]+)(?:#[^)]*)?\)", document.read_text("utf-8")
        ):
            if target.startswith(("http://", "https://")):
                continue
            assert (document.parent / target).resolve().is_file(), (
                f"{document.name}: broken link {target}"
            )


ADR_FILES = sorted(ADR.glob("0*.md"))


def test_there_are_decision_records():
    assert len(ADR_FILES) >= 8


@pytest.mark.parametrize("record", ADR_FILES, ids=lambda p: p.name)
def test_each_decision_record_has_every_section_and_a_title_matching_its_number(record):
    text = record.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        assert section in text, f"{record.name} is missing {section}"
    number = record.name[:4]
    assert text.startswith(f"# {number}."), (
        f"{record.name}: title must start '# {number}.'"
    )
    assert re.search(r"\*\*Status:\*\* (Accepted|Superseded|Proposed)", text)


def test_the_index_lists_every_record_and_only_real_ones():
    index = (ADR / "README.md").read_text(encoding="utf-8")
    listed = re.findall(r"\]\((0\d{3}-[a-z0-9-]+\.md)\)", index)
    assert sorted(listed) == [p.name for p in ADR_FILES]
    numbers = [p.name[:4] for p in ADR_FILES]
    assert numbers == [f"{i:04d}" for i in range(1, len(numbers) + 1)], (
        "numbers must be consecutive"
    )


def test_the_readme_links_the_runbooks_and_the_decision_records():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/RUNBOOKS.md" in readme and "docs/adr/README.md" in readme


def test_the_runbooks_state_what_teardown_destroys():
    text = RUNBOOKS.read_text(encoding="utf-8")
    assert "including the database" in text and "7-day database backups" in text
    # and the facts they quote match the code
    config = (ROOT / "src" / "leyline" / "config.py").read_text(encoding="utf-8")
    assert (
        "session_idle_seconds: int = Field(default=30 * 60" in config
    )  # "30 minutes idle"
    assert (
        "session_absolute_seconds: int = Field(default=12 * 60 * 60" in config
    )  # "12 hours"
    assert (
        "llm_breaker_reset_seconds: float = Field(default=30" in config
    )  # "30 seconds"
    assert "ingest_max_attempts: int = Field(default=5" in config  # "up to 5 times"
