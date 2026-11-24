"""The follow-up evaluation: does skipping the rewrite keep retrieval quality?"""

import json
import uuid
from pathlib import Path

import pytest

from leyline.evals.followups import (
    format_followups,
    load_followups,
    run_followups,
)
from leyline.evals.golden import GoldenError, load_acl_oracle
from leyline.ports.search import SearchHit

ROOT = Path(__file__).resolve().parent.parent
FOLLOWUPS = ROOT / "evals" / "followups.jsonl"
CORPUS = ROOT / "corpus"

ORACLE = {
    "PUB-1": frozenset({"*"}),
    "PUB-2": frozenset({"*"}),
    "SEC-1": frozenset({"security"}),
}
IDS = {ref: uuid.uuid4() for ref in ORACLE}
REFS = {document_id: ref for ref, document_id in IDS.items()}


def write(tmp_path, *lines):
    path = tmp_path / "followups.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def case(**overrides):
    base = {
        "id": "x1",
        "persona": "employee",
        "kind": "answerable",
        "history": ["How do I connect to the VPN?"],
        "message": "and on a Mac?",
        "standalone": "How do I connect to the VPN on a Mac?",
        "needs_rewrite": True,
        "expect": ["PUB-1"],
    }
    base.update(overrides)
    return base


def hit(ref):
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=IDS[ref],
        version_id=uuid.uuid4(),
        title=ref,
        heading=None,
        content="x",
        score=1.0,
    )


class ScriptedIndex:
    def __init__(self, script):
        self.script = script
        self.queries = []

    def search(self, principal, query, *, limit):
        self.queries.append(query)
        return [hit(ref) for ref in self.script.get(query, [])]


# --- the shipped set ---------------------------------------------------------------------


def test_the_shipped_follow_up_set_is_valid_for_the_shipped_corpus():
    cases = load_followups(FOLLOWUPS, load_acl_oracle(CORPUS))
    assert len(cases) >= 30
    assert {c.kind for c in cases} == {"answerable", "restricted"}
    assert len({c.persona for c in cases}) >= 3
    assert any(c.needs_rewrite for c in cases) and any(
        not c.needs_rewrite for c in cases
    )


# --- validation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (case(history=[]), "needs earlier questions"),
        (case(standalone=" "), "empty message or standalone"),
        (case(expect=["NOPE-1"]), "unknown document"),
        (case(persona="employee", expect=["SEC-1"]), "cannot read"),
        (case(kind="restricted", expect=[], forbid=["PUB-1"]), "CAN read"),
        (case(kind="restricted", expect=[]), "need only 'forbid'"),
        (case(expect=[]), "need only 'expect'"),
        (case(kind="bogus"), "unknown kind"),
    ],
)
def test_inconsistent_follow_up_cases_are_rejected(tmp_path, line, message):
    with pytest.raises(GoldenError, match=message):
        load_followups(write(tmp_path, line), ORACLE)


def test_empty_files_duplicate_ids_and_missing_fields_are_rejected(tmp_path):
    with pytest.raises(GoldenError, match="no cases"):
        load_followups(write(tmp_path), ORACLE)
    with pytest.raises(GoldenError, match="duplicate id"):
        load_followups(write(tmp_path, case(), case()), ORACLE)
    incomplete = case()
    del incomplete["standalone"]
    with pytest.raises(GoldenError, match="invalid case"):
        load_followups(write(tmp_path, incomplete), ORACLE)


# --- the measurement ---------------------------------------------------------------------


def cases_from(tmp_path, *lines):
    return load_followups(write(tmp_path, *lines), ORACLE)


def test_the_policy_searches_the_rewrite_only_when_the_question_needs_one(tmp_path):
    cases = cases_from(
        tmp_path,
        case(id="dep"),  # "and on a Mac?" needs the rewrite
        case(
            id="alone",
            message="How do I get a guest into the building?",
            standalone="How do I get a guest into the building?",
            needs_rewrite=False,
            expect=["PUB-2"],
        ),
    )
    index = ScriptedIndex(
        {
            "How do I connect to the VPN on a Mac?": ["PUB-1"],
            "How do I get a guest into the building?": ["PUB-2"],
        }
    )

    report = run_followups(index, cases, refs=REFS, acl=ORACLE)

    s = report.summary
    assert s["never_recall@5"] == 0.5  # "and on a Mac?" finds nothing as typed
    assert s["always_recall@5"] == 1.0
    assert s["policy_recall@5"] == 1.0
    assert s["skip_rate"] == 0.5
    assert s["policy_agrees_with_author"] == 1.0
    assert report.misses == []


def test_a_wrongly_skipped_rewrite_shows_up_as_a_policy_miss(tmp_path):
    # Stands alone to the check (no reference words) but the author says it does not.
    leaning = case(
        id="lean",
        message="How long does the approval take for staff?",
        standalone="How long does expense approval take for staff?",
    )
    index = ScriptedIndex({"How long does expense approval take for staff?": ["PUB-1"]})

    report = run_followups(index, cases_from(tmp_path, leaning), refs=REFS, acl=ORACLE)

    assert report.summary["policy_recall@5"] == 0.0
    assert report.summary["always_recall@5"] == 1.0
    assert report.summary["policy_agrees_with_author"] == 0.0
    assert "[lean] policy missed" in report.misses[0]


def test_a_restricted_follow_up_must_not_return_the_forbidden_document(tmp_path):
    restricted = case(
        id="r1", kind="restricted", expect=[], forbid=["SEC-1"], persona="employee"
    )
    clean = ScriptedIndex({})
    leaky = ScriptedIndex({"How do I connect to the VPN on a Mac?": ["SEC-1"]})

    ok = run_followups(clean, cases_from(tmp_path, restricted), refs=REFS, acl=ORACLE)
    bad = run_followups(leaky, cases_from(tmp_path, restricted), refs=REFS, acl=ORACLE)

    assert ok.summary["restricted_pass_rate"] == 1.0 and ok.summary["acl_leaks"] == 0
    assert bad.summary["restricted_pass_rate"] == 0.0
    assert bad.summary["acl_leaks"] >= 1  # the employee may not read SEC-1


def test_an_acl_leak_in_any_policy_is_counted(tmp_path):
    index = ScriptedIndex({"and on a Mac?": ["SEC-1"]})
    report = run_followups(index, cases_from(tmp_path, case()), refs=REFS, acl=ORACLE)
    assert report.summary["acl_leaks"] == 1  # only the "never" policy searched it


def test_the_report_is_printable(tmp_path):
    report = run_followups(
        ScriptedIndex({}), cases_from(tmp_path, case()), refs=REFS, acl=ORACLE
    )
    text = format_followups(report)
    assert "never" in text and "policy" in text and "acl_leaks" in text
