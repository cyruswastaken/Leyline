"""The offline retrieval gate for the Azure deployment (text-embedding-3-small).

The shipped defaults are calibrated for nomic-embed-text; with them the Azure model
found the right document for only 61% of questions and users saw "I couldn't find
relevant information" for everything. The floors the template sets for Azure were
chosen from a sweep (docs/EVALUATION.md); this gate replays the recorded Azure
embeddings with exactly those floors, so a change that hurts Azure retrieval or leaks
a restricted document fails CI without a model or a network.
"""

import dataclasses
import json
import re
import threading
from pathlib import Path

import pytest

from leyline.adapters.search_postgres import PostgresSearchIndex, SearchOptions
from leyline.evals import __main__ as evals_cli
from leyline.evals.embeddings import EmbeddingCache, ReplayEmbedder
from leyline.evals.golden import load_acl_oracle, load_golden
from leyline.evals.runner import check_thresholds, format_report, run_eval
from leyline.ingestion.service import IngestionService
from leyline.seed import seed_corpus
from leyline.worker import run_worker
from tests.support import make_settings

ROOT = Path(__file__).resolve().parent.parent
EMBEDDINGS = ROOT / "evals" / "embeddings.azure.json"
THRESHOLDS = ROOT / "evals" / "thresholds.azure.json"
APPS = ROOT / "infra" / "modules" / "apps.bicep"
GOLDEN = ROOT / "evals" / "golden.jsonl"
CORPUS = ROOT / "corpus"


def deployed_floor(name: str) -> float:
    """The floor the Azure template really sets, so the gate cannot drift from it."""
    match = re.search(rf"name: '{name}', value: '([0-9.]+)'", APPS.read_text("utf-8"))
    assert match, f"{name} is not set in infra/modules/apps.bicep"
    return float(match.group(1))


@pytest.fixture
def azure_corpus(db, blobs):
    settings = make_settings(
        embedding_document_prefix="",
        embedding_query_prefix="",
        search_min_similarity=deployed_floor("SEARCH_MIN_SIMILARITY"),
        search_min_lexical=deployed_floor("SEARCH_MIN_LEXICAL"),
    )
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
        "the recorded Azure embeddings no longer match the corpus or chunker; re-record "
        "with EMBEDDING_* pointing at Azure OpenAI: python -m leyline.evals --index hybrid "
        f"--seed --reindex --record-embeddings evals/embeddings.azure.json ({failed[:2]})"
    )
    return db, embedder, settings


def run_azure(azure_corpus):
    db, embedder, settings = azure_corpus
    index = PostgresSearchIndex(
        db,
        embedder,
        dataclasses.replace(
            SearchOptions.from_settings(settings), strict_embedding=True
        ),
    )
    oracle = load_acl_oracle(CORPUS)
    return run_eval(
        index,
        load_golden(GOLDEN, oracle),
        refs=evals_cli.document_refs(db),
        acl=oracle,
    )


def test_the_azure_floors_are_not_the_nomic_defaults():
    # With the nomic floor (0.58) this model refuses about 40% of answerable questions.
    assert deployed_floor("SEARCH_MIN_SIMILARITY") < 0.58


def test_the_committed_azure_cache_is_for_the_deployed_model_and_size():
    cache = EmbeddingCache.load(EMBEDDINGS)
    assert cache.model == "text-embedding-3-small"
    assert len(cache) >= 200
    assert {len(v) for v in cache.vectors.values()} == {768}


def test_azure_hybrid_search_meets_the_floor_and_never_leaks(azure_corpus):
    report = run_azure(azure_corpus)
    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))["hybrid"]
    assert report.summary["acl_leaks"] == 0, report.leaks
    assert check_thresholds(report.summary, thresholds) == [], format_report(report)


def test_the_azure_gate_catches_a_broken_permission_filter(azure_corpus, monkeypatch):
    from sqlalchemy import true

    import leyline.adapters.search_postgres as search_module

    monkeypatch.setattr(search_module, "readable_by", lambda principal: true())
    report = run_azure(azure_corpus)
    assert report.summary["acl_leaks"] > 0
    assert report.summary["restricted_pass_rate"] < 1.0


def test_the_old_floor_would_fail_this_gate(azure_corpus):
    """Proves the gate has teeth: the nomic floor on the Azure model misses the bar."""
    db, embedder, settings = azure_corpus
    strict = dataclasses.replace(
        SearchOptions.from_settings(settings),
        min_similarity=0.58,
        strict_embedding=True,
    )
    oracle = load_acl_oracle(CORPUS)
    report = run_eval(
        PostgresSearchIndex(db, embedder, strict),
        load_golden(GOLDEN, oracle),
        refs=evals_cli.document_refs(db),
        acl=oracle,
    )
    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))["hybrid"]
    assert check_thresholds(report.summary, thresholds) != []
