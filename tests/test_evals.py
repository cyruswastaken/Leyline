"""The retrieval evaluation harness and the quality / ACL-leak gate."""

import dataclasses
import json
import threading
import uuid
from pathlib import Path

import pytest

from leyline.adapters.search_postgres import PostgresSearchIndex, SearchOptions
from leyline.auth.principal import Principal
from leyline.config import Settings
from leyline.evals import __main__ as evals_cli
from leyline.evals.embeddings import EmbeddingCache, ReplayEmbedder
from leyline.evals.golden import (
    PERSONAS,
    GoldenCase,
    GoldenError,
    load_acl_oracle,
    load_golden,
    persona_principal,
)
from leyline.evals.runner import (
    check_thresholds,
    format_report,
    ordered_documents,
    run_eval,
)
from leyline.ingestion.service import IngestionService
from leyline.ports.search import SearchHit
from leyline.seed import seed_corpus
from leyline.worker import run_worker
from tests.support import make_settings

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "evals" / "golden.jsonl"
THRESHOLDS = ROOT / "evals" / "thresholds.json"
CORPUS = ROOT / "corpus"

ORACLE = {
    "PUB-1": frozenset({"*"}),
    "PUB-2": frozenset({"*"}),
    "HR-1": frozenset({"hr"}),
    "SEC-1": frozenset({"security"}),
}


def write_golden(tmp_path, *lines):
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def case(**overrides):
    base = {"id": "c1", "persona": "employee", "kind": "answerable", "question": "q"}
    base.update(overrides)
    return base


# --- the shipped golden set -----------------------------------------------------------


@pytest.fixture(scope="module")
def real_oracle():
    return load_acl_oracle(CORPUS)


def test_the_shipped_golden_set_is_valid_for_the_shipped_corpus(real_oracle):
    cases = load_golden(GOLDEN, real_oracle)
    kinds = {c.kind for c in cases}
    assert kinds == {"answerable", "restricted", "unanswerable"}
    assert len(cases) >= 75
    assert {c.persona for c in cases} == set(PERSONAS)


def test_every_corpus_document_is_covered_by_the_golden_set(real_oracle):
    referenced = {
        ref for c in load_golden(GOLDEN, real_oracle) for ref in (*c.expect, *c.forbid)
    }
    assert referenced == set(real_oracle)


def test_the_oracle_matches_the_corpus_files(real_oracle):
    assert len(real_oracle) >= 49
    assert real_oracle["RES-NETWORK-001"] == {"security"}
    assert real_oracle["GEN-VPN-001"] == {"*"}
    assert real_oracle["HR-PAY-001"] == {"hr"}


# --- golden validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (case(persona="intern", expect=["PUB-1"]), "unknown persona"),
        (case(expect=["NOPE-9"]), "unknown document"),
        (case(expect=["HR-1"]), "cannot read expected document"),
        (case(), "need 'expect'"),
        (case(expect=["PUB-1"], forbid=["HR-1"]), "no 'forbid'"),
        (case(kind="restricted", forbid=["PUB-1"]), "CAN read"),
        (case(kind="restricted"), "need 'forbid'"),
        (case(kind="unanswerable", expect=["PUB-1"]), "take no"),
        (case(kind="bogus", expect=["PUB-1"]), "unknown kind"),
        (case(expect=["PUB-1"], question="  "), "empty question"),
    ],
)
def test_inconsistent_golden_cases_are_rejected(tmp_path, line, message):
    with pytest.raises(GoldenError, match=message):
        load_golden(write_golden(tmp_path, line), ORACLE)


def test_duplicate_ids_empty_files_and_bad_json_are_rejected(tmp_path):
    ok = case(expect=["PUB-1"])
    with pytest.raises(GoldenError, match="duplicate id"):
        load_golden(write_golden(tmp_path, ok, ok), ORACLE)
    with pytest.raises(GoldenError, match="no cases"):
        load_golden(write_golden(tmp_path), ORACLE)
    broken = tmp_path / "bad.jsonl"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(GoldenError, match="invalid case"):
        load_golden(broken, ORACLE)
    missing_field = write_golden(tmp_path, {"id": "x"})
    with pytest.raises(GoldenError, match="invalid case"):
        load_golden(missing_field, ORACLE)


def test_comments_and_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "g.jsonl"
    path.write_text(
        "# a comment\n\n" + json.dumps(case(expect=["PUB-1"])) + "\n", encoding="utf-8"
    )
    assert len(load_golden(path, ORACLE)) == 1


def test_personas_match_the_keycloak_demo_users():
    assert persona_principal("security").groups == {"staff", "security"}
    assert persona_principal("employee").groups == {"staff"}
    with pytest.raises(GoldenError):
        persona_principal("nobody")


# --- metrics ------------------------------------------------------------------------------

IDS = {ref: uuid.uuid4() for ref in ORACLE}
REFS = {doc_id: ref for ref, doc_id in IDS.items()}


def hit(ref, score=1.0):
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=IDS[ref],
        version_id=uuid.uuid4(),
        title=ref,
        heading=None,
        content="x",
        score=score,
    )


class ScriptedIndex:
    """Returns canned hits per question and records who asked."""

    def __init__(self, script):
        self.script = script
        self.asked: list[tuple[Principal, str]] = []

    def search(self, principal, query, *, limit):
        self.asked.append((principal, query))
        return list(self.script.get(query, []))


def cases_for_metrics():
    return [
        GoldenCase("a1", "employee", "q-first", "answerable", expect=("PUB-1",)),
        GoldenCase("a2", "employee", "q-third", "answerable", expect=("PUB-2",)),
        GoldenCase("a3", "employee", "q-miss", "answerable", expect=("PUB-1",)),
        GoldenCase("r1", "employee", "q-clean", "restricted", forbid=("HR-1",)),
        GoldenCase("u1", "employee", "q-none", "unanswerable"),
        GoldenCase("u2", "employee", "q-junk", "unanswerable"),
    ]


def test_metrics_are_computed_exactly():
    index = ScriptedIndex(
        {
            "q-first": [hit("PUB-1", 3.0), hit("PUB-2")],
            "q-third": [hit("PUB-1"), hit("PUB-1"), hit("PUB-2", 0.5)],  # dup chunks
            "q-miss": [hit("PUB-2")],
            "q-clean": [hit("PUB-1")],
            "q-none": [],
            "q-junk": [hit("PUB-2", 0.1)],
        }
    )

    report = run_eval(index, cases_for_metrics(), refs=REFS, acl=ORACLE)
    s = report.summary

    # a1 rank 1, a2 rank 2 (chunks of one document collapse), a3 never found
    assert s["recall@1"] == pytest.approx(1 / 3)
    assert s["recall@3"] == pytest.approx(2 / 3)
    assert s["recall@5"] == pytest.approx(2 / 3)
    assert s["mrr"] == pytest.approx((1 + 0.5 + 0) / 3)
    assert s["refusal_rate"] == 0.5
    assert s["restricted_pass_rate"] == 1.0
    assert s["acl_leaks"] == 0
    # top score of the first returned document for each answerable case
    assert report.score_samples["answerable"] == [1.0, 1.0, 3.0]
    assert {r.case.id for r in report.failures} == {"a3", "u2"}


def test_chunks_of_one_document_collapse_in_rank_order():
    hits = [hit("PUB-1", 3), hit("PUB-1", 2), hit("PUB-2", 1), hit("PUB-1", 0.5)]
    refs, scores = ordered_documents(hits, REFS)
    assert refs == ["PUB-1", "PUB-2"] and scores == [3, 1]


def test_each_question_is_asked_as_the_cases_persona():
    index = ScriptedIndex({})
    run_eval(index, cases_for_metrics()[:1], refs=REFS, acl=ORACLE)
    principal, question = index.asked[0]
    assert question == "q-first" and principal.groups == {"staff"}


def test_a_search_that_ignores_permissions_is_caught_as_an_acl_leak():
    """The gate itself: a leaky index must fail the evaluation loudly."""
    leaky = ScriptedIndex(
        {
            "q-first": [hit("PUB-1"), hit("SEC-1")],  # employee must not get SEC-1
            "q-clean": [hit("HR-1")],  # ...nor HR-1
        }
    )

    report = run_eval(leaky, cases_for_metrics(), refs=REFS, acl=ORACLE)

    assert report.summary["acl_leaks"] == 2
    assert sorted(report.leaks) == [("a1", "SEC-1"), ("r1", "HR-1")]
    leaked_cases = {r.case.id for r in report.failures if r.leaked}
    assert leaked_cases == {"a1", "r1"}
    assert report.summary["restricted_pass_rate"] == 0.0
    assert "ACL LEAK" in format_report(report)


def test_an_unknown_document_in_results_counts_as_a_leak():
    index = ScriptedIndex({"q-first": [hit("PUB-1")]})
    stray = SearchHit(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "?", None, "x", 1.0)
    index.script["q-first"].append(stray)
    report = run_eval(index, cases_for_metrics()[:1], refs=REFS, acl=ORACLE)
    assert report.summary["acl_leaks"] == 1


# --- thresholds -----------------------------------------------------------------------------


def test_thresholds_report_regressions_in_both_directions():
    summary = {"recall@5": 0.70, "acl_leaks": 1.0, "mrr": 0.9}
    problems = check_thresholds(
        summary, {"min": {"recall@5": 0.8, "mrr": 0.5}, "max": {"acl_leaks": 0}}
    )
    assert len(problems) == 2
    assert any("recall@5" in p and "below" in p for p in problems)
    assert any("acl_leaks" in p and "above" in p for p in problems)


def test_meeting_a_threshold_exactly_passes_and_unmeasured_metrics_fail():
    assert check_thresholds({"mrr": 0.5}, {"min": {"mrr": 0.5}}) == []
    assert check_thresholds({}, {"min": {"mrr": 0.5}}) == ["mrr: not measured"]
    assert check_thresholds({}, {"max": {"acl_leaks": 0}}) == [
        "acl_leaks: not measured"
    ]


def test_the_shipped_thresholds_demand_zero_leaks_and_perfect_restriction():
    fts = json.loads(THRESHOLDS.read_text(encoding="utf-8"))["fts"]
    assert fts["max"]["acl_leaks"] == 0
    assert fts["min"]["restricted_pass_rate"] == 1.0


# --- against the real database and corpus (the CI gate) ------------------------------------


@pytest.fixture
def indexed_corpus(db, ingestion, drain):
    seed_corpus(ingestion, CORPUS)
    drain()
    return db


def run_real(db, index):
    oracle = load_acl_oracle(CORPUS)
    return run_eval(
        index,
        load_golden(GOLDEN, oracle),
        refs=evals_cli.document_refs(db),
        acl=oracle,
    )


def test_keyword_search_meets_the_recorded_floor_and_never_leaks(indexed_corpus):
    report = run_real(indexed_corpus, PostgresSearchIndex(indexed_corpus))

    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))["fts"]
    assert report.summary["acl_leaks"] == 0, report.leaks
    assert check_thresholds(report.summary, thresholds) == [], format_report(report)


def test_the_gate_catches_a_database_search_whose_filter_is_broken(
    indexed_corpus, monkeypatch
):
    """Disable the ACL predicate and prove the evaluation notices."""
    from sqlalchemy import true

    import leyline.adapters.search_postgres as search_module

    monkeypatch.setattr(search_module, "readable_by", lambda principal: true())

    report = run_real(indexed_corpus, PostgresSearchIndex(indexed_corpus))

    assert report.summary["acl_leaks"] > 0
    assert report.summary["restricted_pass_rate"] < 1.0


# --- the command line ---------------------------------------------------------------------------


@pytest.fixture
def cli(indexed_corpus, monkeypatch):
    monkeypatch.setattr(
        evals_cli, "get_settings", lambda: make_settings(corpus_dir=CORPUS)
    )
    monkeypatch.setattr(evals_cli, "build_database", lambda settings: indexed_corpus)
    return evals_cli.main


def test_the_cli_passes_when_thresholds_are_met(cli, capsys):
    assert (
        cli(["--check", str(THRESHOLDS), "--json", str(ROOT / "evals" / ".out.json")])
        == 0
    )
    output = capsys.readouterr().out
    assert "recall@5" in output and "thresholds: all met" in output
    out = ROOT / "evals" / ".out.json"
    try:
        saved = json.loads(out.read_text(encoding="utf-8"))
        assert saved["summary"]["acl_leaks"] == 0 and saved["index"] == "fts"
    finally:
        out.unlink()


def test_the_cli_fails_on_a_threshold_regression(cli, tmp_path, capsys):
    impossible = tmp_path / "t.json"
    impossible.write_text(
        json.dumps({"fts": {"min": {"recall@1": 1.01}, "max": {}}}), encoding="utf-8"
    )
    assert cli(["--check", str(impossible)]) == 1
    assert "below the minimum" in capsys.readouterr().err


def test_the_cli_fails_on_any_acl_leak_even_without_thresholds(
    cli, monkeypatch, capsys
):
    from sqlalchemy import true

    import leyline.adapters.search_postgres as search_module

    monkeypatch.setattr(search_module, "readable_by", lambda principal: true())
    assert cli([]) == 1
    assert "ACL leaks found" in capsys.readouterr().err


def test_the_cli_reports_unusable_environments_with_status_2(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        evals_cli, "get_settings", lambda: make_settings(corpus_dir=CORPUS)
    )

    monkeypatch.setattr(evals_cli, "build_database", lambda settings: None)
    assert evals_cli.main([]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_the_cli_rejects_a_broken_golden_file(cli, tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(case(expect=["NOPE-1"])), encoding="utf-8")
    assert cli(["--golden", str(bad)]) == 2
    assert "golden set error" in capsys.readouterr().err


def test_the_cli_refuses_to_run_against_an_empty_index(db, monkeypatch, capsys):
    monkeypatch.setattr(
        evals_cli, "get_settings", lambda: make_settings(corpus_dir=CORPUS)
    )
    monkeypatch.setattr(evals_cli, "build_database", lambda settings: db)
    assert evals_cli.main([]) == 2
    assert "no documents are indexed" in capsys.readouterr().err


# --- hybrid search: the offline CI gate ------------------------------------------------------
#
# Real embeddings are recorded once into evals/embeddings.json (see
# docs/EVALUATION.md) and replayed here, so CI measures the real model's
# semantic quality with no model and no network.

EMBEDDINGS = ROOT / "evals" / "embeddings.json"
DEFAULTS = Settings(_env_file=None)


def hybrid_settings(**overrides):
    """The shipped defaults, plus the task prefixes the cache was recorded with."""
    return make_settings(
        embedding_document_prefix="search_document: ",
        embedding_query_prefix="search_query: ",
        search_min_similarity=DEFAULTS.search_min_similarity,
        search_min_lexical=DEFAULTS.search_min_lexical,
        **overrides,
    )


@pytest.fixture
def hybrid_corpus(db, blobs):
    settings = hybrid_settings()
    embedder = ReplayEmbedder(EmbeddingCache.load(EMBEDDINGS))
    service = IngestionService(db, blobs, embedder, settings)
    seed_corpus(service, CORPUS)
    run_worker(service, db, settings, stop=threading.Event(), once=True)

    failed = [
        f"{d.title}: {d.latest_version.error}"
        for d in service.list_documents()
        if d.latest_version.status == "failed"
    ]
    assert not failed, (
        "the recorded embeddings no longer match the corpus or chunker; re-record: "
        "python -m leyline.evals --index hybrid --seed --reindex "
        f"--record-embeddings evals/embeddings.json ({failed[:2]})"
    )
    return db, embedder, settings


def run_hybrid(hybrid_corpus):
    db, embedder, settings = hybrid_corpus
    index = PostgresSearchIndex(
        db,
        embedder,
        dataclasses.replace(
            SearchOptions.from_settings(settings), strict_embedding=True
        ),
    )
    return run_real(db, index)


def test_the_shipped_defaults_are_the_calibrated_floors():
    assert DEFAULTS.search_min_similarity == 0.58
    assert DEFAULTS.search_min_lexical == 0.5
    assert DEFAULTS.search_max_chunks_per_document == 2


def test_hybrid_search_meets_the_recorded_floor_and_never_leaks(hybrid_corpus):
    report = run_hybrid(hybrid_corpus)

    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))["hybrid"]
    assert report.summary["acl_leaks"] == 0, report.leaks
    assert check_thresholds(report.summary, thresholds) == [], format_report(report)


def test_hybrid_search_is_clearly_better_than_keyword_search(hybrid_corpus):
    """Guards against the semantic leg silently switching off."""
    hybrid = run_hybrid(hybrid_corpus).summary
    keyword = run_real(hybrid_corpus[0], PostgresSearchIndex(hybrid_corpus[0])).summary

    assert hybrid["recall@5"] >= keyword["recall@5"] + 0.10
    assert hybrid["mrr"] >= keyword["mrr"] + 0.10
    assert hybrid["refusal_rate"] > keyword["refusal_rate"]


def test_the_hybrid_gate_catches_a_broken_permission_filter(hybrid_corpus, monkeypatch):
    from sqlalchemy import true

    import leyline.adapters.search_postgres as search_module

    monkeypatch.setattr(search_module, "readable_by", lambda principal: true())

    report = run_hybrid(hybrid_corpus)

    assert report.summary["acl_leaks"] > 0
    assert report.summary["restricted_pass_rate"] < 1.0


def test_the_committed_cache_is_for_the_expected_model_and_size():
    cache = EmbeddingCache.load(EMBEDDINGS)
    assert cache.model == "nomic-embed-text"
    assert len(cache) >= 200
    assert {len(v) for v in cache.vectors.values()} == {768}
