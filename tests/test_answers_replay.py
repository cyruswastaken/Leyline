"""Answer quality in CI: recorded model answers through today's pipeline.

The recorded answers (evals/answers.azure.json) are a live gpt-5.4-mini's replies to
the golden questions. Replaying them runs everything Leyline does around a model
(retrieval with the recorded Azure embeddings, authorization, the context and the
prompt, redaction, citation verification, the refusal rules) and grades the result,
so a regression in any of that fails CI without a model or a network.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from leyline.adapters.search_postgres import PostgresSearchIndex, SearchOptions
from leyline.evals import __main__ as evals_cli
from leyline.evals.answer_cache import AnswerCache, MissingAnswerError, ReplayChatModel
from leyline.evals.answers import format_report, run_answer_eval
from leyline.evals.golden import load_acl_oracle, load_golden
from leyline.evals.runner import check_thresholds
from tests.test_evals_azure import azure_corpus  # noqa: F401  (a fixture)

ROOT = Path(__file__).resolve().parent.parent
ANSWERS = ROOT / "evals" / "answers.azure.json"
THRESHOLDS = ROOT / "evals" / "thresholds.answers.azure.json"
GOLDEN = ROOT / "evals" / "golden.jsonl"
CORPUS = ROOT / "corpus"


def run(azure_corpus, cases=None):  # noqa: F811
    db, embedder, settings = azure_corpus
    search = PostgresSearchIndex(
        db,
        embedder,
        dataclasses.replace(
            SearchOptions.from_settings(settings), strict_embedding=True
        ),
    )
    oracle = load_acl_oracle(CORPUS)
    return run_answer_eval(
        cases or load_golden(GOLDEN, oracle),
        search=search,
        database=db,
        model=ReplayChatModel(AnswerCache.load(ANSWERS)),
        settings=settings,
        refs=evals_cli.document_refs(db),
        acl=oracle,
    )


def test_the_committed_answers_are_for_the_deployed_chat_model():
    cache = AnswerCache.load(ANSWERS)
    assert cache.model == "gpt-5.4-mini"
    assert len(cache) >= 90
    assert len(cache.prompt_version) == 12


def test_the_recorded_answers_still_meet_the_floors_and_never_leak(azure_corpus):  # noqa: F811
    report = run(azure_corpus)
    floors = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    assert report.summary["acl_leaks"] == 0
    assert check_thresholds(report.summary, floors) == [], format_report(
        report, verbose=True
    )
    assert int(report.summary["cases"]) == len(
        load_golden(GOLDEN, load_acl_oracle(CORPUS))
    )


def test_a_changed_prompt_fails_loudly_instead_of_grading_something_else(
    azure_corpus,  # noqa: F811
    monkeypatch,
):
    import leyline.services.llm as llm

    monkeypatch.setattr(llm, "SYSTEM_PROMPT", llm.SYSTEM_PROMPT + "\nBe brief.")
    with pytest.raises(MissingAnswerError, match="Re-record"):
        run(azure_corpus)


def test_a_pipeline_that_leaks_a_forbidden_source_fails_the_gate(
    azure_corpus,  # noqa: F811
    monkeypatch,
):
    """Proves the gate has teeth: with the permission filter off, restricted
    documents reach the prompt and the replay reports the leak."""
    from sqlalchemy import true

    import leyline.adapters.search_postgres as search_module

    monkeypatch.setattr(search_module, "readable_by", lambda principal: true())
    oracle = load_acl_oracle(CORPUS)
    restricted = [c for c in load_golden(GOLDEN, oracle) if c.kind == "restricted"]
    try:
        report = run(azure_corpus, restricted)
    except MissingAnswerError:
        # The leaked context changed the prompt, so nothing was recorded for it:
        # that is itself the tripwire firing.
        return
    assert report.summary["acl_leaks"] > 0 or report.summary["restricted_pass"] < 1.0
