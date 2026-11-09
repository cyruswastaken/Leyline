"""Embedding record/replay and the hybrid paths of the evaluation CLI."""

import json
import threading
import time
from array import array
from pathlib import Path

import pytest
from sqlalchemy import update

from leyline.adapters.blob_memory import InMemoryBlobStore
from leyline.db.models import IngestionJob
from leyline.evals import __main__ as evals_cli
from leyline.evals.embeddings import (
    EmbeddingCache,
    MissingEmbeddingError,
    RecordingEmbedder,
    ReplayEmbedder,
    text_key,
)
from tests.support import (
    DEFAULT_CONCEPTS,
    ConceptEmbedder,
    FakeEmbedder,
    make_settings,
)

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"


class CountingEmbedder:
    model = "counting-model"

    def __init__(self):
        self.batches: list[list[str]] = []

    def embed(self, texts):
        self.batches.append(list(texts))
        return [[float(len(t)), 0.5, -1.25] for t in texts]


# --- cache ------------------------------------------------------------------------------


def test_the_cache_round_trips_vectors_exactly(tmp_path):
    vectors = {text_key("a"): [0.25, -1.5, 3.0], text_key("b"): [0.0, 1.0, 2.0]}
    path = tmp_path / "cache.json"
    EmbeddingCache("m1", vectors).save(path)

    loaded = EmbeddingCache.load(path)

    assert loaded.model == "m1" and loaded.vectors == vectors
    assert len(loaded) == 2


def test_float32_values_survive_the_binary_encoding(tmp_path):
    exact = list(array("f", [0.1, 1 / 3, -2.5e-7, 12345.678]))  # already float32
    EmbeddingCache("m", {text_key("x"): exact}).save(tmp_path / "c.json")
    assert EmbeddingCache.load(tmp_path / "c.json").vectors[text_key("x")] == exact


def test_the_saved_file_is_deterministic_and_compact(tmp_path):
    first = {text_key("b"): [1.0], text_key("a"): [2.0]}
    EmbeddingCache("m", first).save(tmp_path / "1.json")
    EmbeddingCache("m", dict(reversed(first.items()))).save(tmp_path / "2.json")
    assert (tmp_path / "1.json").read_text() == (tmp_path / "2.json").read_text()
    assert "\n  " not in (tmp_path / "1.json").read_text()  # not pretty-printed


def test_an_unknown_cache_format_is_rejected(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"format": 99, "model": "m", "vectors": {}}))
    with pytest.raises(ValueError, match="unsupported cache format"):
        EmbeddingCache.load(path)


# --- replay -----------------------------------------------------------------------------------


def test_replay_serves_cached_vectors_and_never_calls_a_model():
    cache = EmbeddingCache("m", {text_key("hello"): [1.0, 2.0]})
    embedder = ReplayEmbedder(cache)
    assert embedder.model == "m"
    assert embedder.embed(["hello", "hello"]) == [[1.0, 2.0], [1.0, 2.0]]


def test_replay_fails_loudly_and_explains_how_to_re_record():
    embedder = ReplayEmbedder(EmbeddingCache("m", {text_key("known"): [1.0]}))
    with pytest.raises(MissingEmbeddingError) as failure:
        embedder.embed(["known", "a brand new chunk of text"])
    message = str(failure.value)
    assert "1 text(s)" in message and "a brand new chunk" in message
    assert "--record-embeddings" in message


def test_replayed_vectors_cannot_be_corrupted_by_callers():
    cache = EmbeddingCache("m", {text_key("x"): [1.0, 2.0]})
    embedder = ReplayEmbedder(cache)
    embedder.embed(["x"])[0][0] = 99.0
    assert embedder.embed(["x"]) == [[1.0, 2.0]]


# --- recording --------------------------------------------------------------------------------


def test_recording_calls_the_model_only_for_texts_it_has_not_seen():
    inner = CountingEmbedder()
    recorder = RecordingEmbedder(inner)

    recorder.embed(["a", "b"])
    recorder.embed(["b", "c", "a"])

    assert inner.batches == [["a", "b"], ["c"]]
    assert len(recorder.cache) == 3


def test_duplicate_texts_in_one_call_are_embedded_once():
    inner = CountingEmbedder()
    out = RecordingEmbedder(inner).embed(["x", "x", "x"])
    assert inner.batches == [["x"]] and out[0] == out[1] == out[2]


def test_recording_extends_an_existing_cache(tmp_path):
    inner = CountingEmbedder()
    existing = EmbeddingCache(inner.model, {text_key("old"): [9.0, 9.0, 9.0]})
    recorder = RecordingEmbedder(inner, existing)

    assert recorder.embed(["old"]) == [[9.0, 9.0, 9.0]]  # reused, not re-embedded
    assert inner.batches == []
    recorder.embed(["new"])
    recorder.cache.save(tmp_path / "c.json")
    assert len(EmbeddingCache.load(tmp_path / "c.json")) == 2


def test_vectors_from_a_different_model_are_never_mixed_into_a_cache():
    inner = CountingEmbedder()
    stale = EmbeddingCache("some-other-model", {text_key("x"): [0.0, 0.0, 0.0]})
    recorder = RecordingEmbedder(inner, stale)
    assert recorder.cache.model == "counting-model" and len(recorder.cache) == 0


# --- hybrid through the CLI ---------------------------------------------------------------------


@pytest.fixture
def cli(db, monkeypatch):
    blobs = InMemoryBlobStore()
    concept = ConceptEmbedder(DEFAULT_CONCEPTS)
    monkeypatch.setattr(
        evals_cli, "get_settings", lambda: make_settings(corpus_dir=CORPUS)
    )
    monkeypatch.setattr(evals_cli, "build_database", lambda settings: db)
    monkeypatch.setattr(evals_cli, "build_blob_store", lambda settings: blobs)
    monkeypatch.setattr(evals_cli, "build_embedder", lambda settings: concept)
    return evals_cli.main


def test_hybrid_needs_an_embedder(db, monkeypatch, capsys):
    monkeypatch.setattr(
        evals_cli, "get_settings", lambda: make_settings(corpus_dir=CORPUS)
    )
    monkeypatch.setattr(evals_cli, "build_database", lambda settings: db)
    monkeypatch.setattr(evals_cli, "build_embedder", lambda settings: None)
    assert evals_cli.main(["--index", "hybrid"]) == 2
    assert "needs an embedder" in capsys.readouterr().err


def test_record_then_replay_gives_identical_results_offline(
    cli, tmp_path, monkeypatch, capsys
):
    recorded = tmp_path / "embeddings.json"
    first_json, second_json = tmp_path / "a.json", tmp_path / "b.json"

    assert (
        cli(
            [
                "--index",
                "hybrid",
                "--seed",
                "--record-embeddings",
                str(recorded),
                "--json",
                str(first_json),
            ]
        )
        == 0
    )
    assert recorded.exists() and "recorded" in capsys.readouterr().out

    # Replay: the real model is gone; only the cache is available.
    monkeypatch.setattr(evals_cli, "build_embedder", lambda settings: None)
    assert (
        cli(
            [
                "--index",
                "hybrid",
                "--reindex",
                "--embeddings",
                str(recorded),
                "--json",
                str(second_json),
            ]
        )
        == 0
    )

    first = json.loads(first_json.read_text())["summary"]
    second = json.loads(second_json.read_text())["summary"]
    assert first == second
    assert second["acl_leaks"] == 0


def test_indexing_with_a_stale_cache_reports_the_failure(cli, tmp_path, capsys):
    stale = tmp_path / "stale.json"
    EmbeddingCache(
        "concept-test-model", {text_key("something else"): [0.0] * 768}
    ).save(stale)

    assert cli(["--index", "hybrid", "--seed", "--embeddings", str(stale)]) == 2

    err = capsys.readouterr().err
    assert "indexing failed" in err and "not in the embedding cache" in err


def test_a_question_missing_from_the_cache_stops_the_run_instead_of_degrading(
    cli, tmp_path, monkeypatch, capsys
):
    recorded = tmp_path / "cache.json"
    assert (
        cli(["--index", "hybrid", "--seed", "--record-embeddings", str(recorded)]) == 0
    )
    capsys.readouterr()

    cache = EmbeddingCache.load(recorded)
    forgotten = text_key("I forgot my login password, how do I get back in?")
    assert forgotten in cache.vectors  # it was recorded...
    del cache.vectors[forgotten]  # ...now make the cache stale
    cache.save(recorded)

    monkeypatch.setattr(evals_cli, "build_embedder", lambda settings: None)
    assert cli(["--index", "hybrid", "--embeddings", str(recorded)]) == 2
    assert "embedding cache error" in capsys.readouterr().err


def test_replay_and_record_cannot_be_combined(cli, tmp_path):
    with pytest.raises(SystemExit):
        cli(
            [
                "--embeddings",
                str(tmp_path / "a"),
                "--record-embeddings",
                str(tmp_path / "b"),
            ]
        )


def test_the_sweep_reports_every_floor_combination_and_no_leaks(cli, tmp_path, capsys):
    out = tmp_path / "sweep.json"
    assert cli(["--index", "hybrid", "--seed", "--sweep", "--json", str(out)]) == 0

    rows = json.loads(out.read_text())["sweep"]
    assert len(rows) == len(evals_cli.SWEEP_SIMILARITY) * len(evals_cli.SWEEP_LEXICAL)
    assert all(row["acl_leaks"] == 0 for row in rows)
    assert "threshold sweep" in capsys.readouterr().out
    # stricter floors can only reduce how much is returned
    loosest = next(
        r for r in rows if r["min_similarity"] == 0.0 and r["min_lexical"] == 0.0
    )
    strictest = rows[-1]
    assert strictest["avg_docs_returned"] <= loosest["avg_docs_returned"]


def test_floor_overrides_reach_the_search(cli, tmp_path, capsys):
    cli(
        ["--index", "hybrid", "--seed", "--min-similarity", "0.9", "--min-lexical", "9"]
    )
    assert "min_similarity=0.9 min_lexical=9.0" in capsys.readouterr().out


def test_the_fake_embedder_is_unusable_for_quality_claims():
    """Guard: hash vectors carry no meaning, so they must not be used to tune floors."""
    a, b = FakeEmbedder().embed(["lost laptop", "stolen computer"])
    cosine = sum(x * y for x, y in zip(a, b, strict=True)) / (
        sum(x * x for x in a) ** 0.5 * sum(y * y for y in b) ** 0.5
    )
    assert abs(cosine) < 0.2  # unrelated, even though the meaning is the same


def test_a_failed_reindex_of_a_live_document_is_reported_not_ignored(
    cli, db, monkeypatch, capsys
):
    """Live documents keep serving when a re-index fails; the CLI must still notice."""
    blobs = InMemoryBlobStore()
    monkeypatch.setattr(evals_cli, "build_blob_store", lambda settings: blobs)
    assert cli(["--index", "fts", "--seed"]) == 0  # everything indexed
    capsys.readouterr()

    for key in list(blobs.keys())[:3]:  # storage loses some originals
        blobs.delete(key)

    assert cli(["--index", "hybrid", "--reindex"]) == 2

    err = capsys.readouterr().err
    assert "indexing failed" in err and "stored file is missing" in err


# --- never measure a database that is still being indexed ---------------------------------


def _hold_a_job_as_another_worker(db, add_doc):
    """A job another worker has claimed and is still processing."""
    add_doc("Busy", "# B\n\nbody", ["*"], index=False)
    with db.session() as session:
        session.execute(
            update(IngestionJob).values(state="running", locked_by="other-worker")
        )


def test_the_cli_waits_while_another_worker_still_holds_a_job(db, add_doc):
    _hold_a_job_as_another_worker(db, add_doc)

    def finish_later():
        time.sleep(0.8)
        with db.session() as session:
            session.execute(update(IngestionJob).values(state="succeeded"))

    thread = threading.Thread(target=finish_later)
    started = time.monotonic()
    thread.start()
    try:
        assert evals_cli.wait_for_idle_queue(db, timeout_seconds=10) is True
    finally:
        thread.join()
    assert time.monotonic() - started >= 0.7  # it really waited


def test_an_idle_queue_returns_immediately(db):
    started = time.monotonic()
    assert evals_cli.wait_for_idle_queue(db, timeout_seconds=10) is True
    assert time.monotonic() - started < 2


def test_a_stuck_job_ends_the_run_with_a_clear_error_not_a_misleading_measurement(
    cli, db, add_doc, capsys
):
    _hold_a_job_as_another_worker(db, add_doc)

    assert cli(["--index", "fts", "--seed", "--wait-seconds", "0.6"]) == 2

    assert "timed out waiting for indexing" in capsys.readouterr().err
