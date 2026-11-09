"""``python -m leyline.evals`` - measure retrieval quality and check for ACL leaks.

Examples::

    python -m leyline.evals --seed                          # import, evaluate keyword
    python -m leyline.evals --index hybrid --embeddings evals/embeddings.json \\
        --seed --reindex --check evals/thresholds.json      # offline, as CI runs it
    python -m leyline.evals --index hybrid --seed --reindex --sweep \\
        --record-embeddings evals/embeddings.json           # real model: calibrate

Exit status: 0 on success; 1 if any ACL leak was found or a threshold was
violated; 2 if the environment is not configured.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from sqlalchemy import func, select

from leyline.adapters.search_postgres import PostgresSearchIndex, SearchOptions
from leyline.config import Settings, get_settings
from leyline.db.database import Database
from leyline.db.models import Document, IngestionJob
from leyline.evals.embeddings import (
    EmbeddingCache,
    MissingEmbeddingError,
    RecordingEmbedder,
    ReplayEmbedder,
)
from leyline.evals.golden import GoldenCase, GoldenError, load_acl_oracle, load_golden
from leyline.evals.runner import (
    DEFAULT_LIMIT,
    EvalReport,
    check_thresholds,
    format_report,
    run_eval,
)
from leyline.ingestion.service import IngestionService
from leyline.ports.embeddings import Embedder
from leyline.ports.search import SearchIndex
from leyline.seed import seed_corpus
from leyline.wiring import (
    build_blob_store,
    build_database,
    build_embedder,
    configure_logging,
)
from leyline.worker import run_worker

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GOLDEN = REPO_ROOT / "evals" / "golden.jsonl"
INDEXES = ("fts", "hybrid")
SWEEP_SIMILARITY = (0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7)
SWEEP_LEXICAL = (0.0, 0.2, 0.4, 0.8)


def document_refs(database: Database) -> dict[uuid.UUID, str]:
    """Database document id -> corpus ``Document ID`` for every live document."""
    with database.session() as session:
        rows = session.execute(
            select(Document.id, Document.source_ref).where(
                Document.deleted_at.is_(None), Document.source_ref.is_not(None)
            )
        ).all()
    return {row.id: row.source_ref for row in rows}


def build_search(
    name: str,
    database: Database,
    embedder: Embedder | None,
    options: SearchOptions,
) -> SearchIndex:
    # Evaluations are strict: a failing embedder must stop the run, not
    # silently turn a hybrid measurement into a keyword one.
    options = dataclasses.replace(options, strict_embedding=True)
    if name == "fts":
        return PostgresSearchIndex(database, None, options)
    if name == "hybrid":
        return PostgresSearchIndex(database, embedder, options)
    raise ValueError(f"unknown search index {name!r}; choose from {INDEXES}")


def choose_embedder(
    args: argparse.Namespace, settings: Settings
) -> tuple[Embedder | None, RecordingEmbedder | None]:
    """Replay a recorded cache, or wrap the real model in a recording memo."""
    if args.embeddings:
        return ReplayEmbedder(EmbeddingCache.load(args.embeddings)), None
    real = build_embedder(settings)
    if real is None:
        return None, None
    path = args.record_embeddings
    existing = EmbeddingCache.load(path) if path and path.exists() else None
    # Always memoise: a sweep asks the same questions many times.
    recorder = RecordingEmbedder(real, existing)
    return recorder, recorder


def _options(args: argparse.Namespace, settings: Settings) -> SearchOptions:
    options = SearchOptions.from_settings(settings)
    overrides = {
        "min_similarity": args.min_similarity,
        "min_lexical": args.min_lexical,
        "max_chunks_per_document": args.max_chunks_per_document,
    }
    return dataclasses.replace(
        options, **{k: v for k, v in overrides.items() if v is not None}
    )


def _sweep(
    index_name: str,
    database: Database,
    embedder: Embedder | None,
    base: SearchOptions,
    cases: Sequence[GoldenCase],
    *,
    refs: Mapping[uuid.UUID, str],
    acl: Mapping[str, frozenset[str]],
    limit: int,
) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for similarity in SWEEP_SIMILARITY:
        for lexical in SWEEP_LEXICAL:
            options = dataclasses.replace(
                base, min_similarity=similarity, min_lexical=lexical
            )
            search = build_search(index_name, database, embedder, options)
            report = run_eval(search, cases, refs=refs, acl=acl, limit=limit)
            rows.append(
                {"min_similarity": similarity, "min_lexical": lexical, **report.summary}
            )
    return rows


def _format_sweep(rows: list[dict[str, float]]) -> str:
    header = "  sim   lex   recall@1 recall@5   mrr   refusal restricted leaks"
    lines = [header]
    for r in rows:
        lines.append(
            f"  {r['min_similarity']:.2f}  {r['min_lexical']:.2f}"
            f"   {r['recall@1']:.3f}    {r['recall@5']:.3f}   {r['mrr']:.3f}"
            f"   {r['refusal_rate']:.3f}     "
            f"{r['restricted_pass_rate']:.3f}    {int(r['acl_leaks'])}"
        )
    return "\n".join(lines)


def wait_for_idle_queue(database: Database, timeout_seconds: float) -> bool:
    """Block until no ingestion job is queued or running.

    ``run_worker(once=True)`` returns when nothing is *claimable*, which can be
    while another worker still holds a job. Evaluating then would measure a
    half-indexed database, so wait for the queue to be genuinely idle.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        with database.session() as session:
            pending = session.scalar(
                select(func.count())
                .select_from(IngestionJob)
                .where(IngestionJob.state.in_(("queued", "running")))
            )
        if not pending:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _failed_documents(service: IngestionService) -> list[str]:
    """Documents whose latest upload failed to index, with the reason."""
    problems: list[str] = []
    for info in service.list_documents():
        latest = info.latest_version
        if latest is None:
            continue
        # A failed re-index of a live document leaves it serving (status stays
        # "indexed"); the failure is recorded on the job.
        job_failed = latest.job is not None and latest.job.state == "failed"
        if latest.status == "failed" or job_failed:
            reason = latest.error or (latest.job.last_error if latest.job else "")
            problems.append(f"{info.title}: {reason}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leyline.evals")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--index", choices=INDEXES, default="fts")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--seed", action="store_true", help="import and index the corpus"
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="re-index every document (use after changing the embedding model)",
    )
    parser.add_argument("--embeddings", type=Path, help="replay this recorded cache")
    parser.add_argument(
        "--record-embeddings", type=Path, help="record the real model's vectors here"
    )
    parser.add_argument("--min-similarity", type=float, default=None)
    parser.add_argument("--min-lexical", type=float, default=None)
    parser.add_argument("--max-chunks-per-document", type=int, default=None)
    parser.add_argument("--sweep", action="store_true", help="grid of relevance floors")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=300.0,
        help="how long to wait for indexing to finish before evaluating",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="list every failing case"
    )
    parser.add_argument(
        "--json", type=Path, default=None, help="write the full report here"
    )
    parser.add_argument(
        "--check", type=Path, default=None, help="thresholds to enforce"
    )
    args = parser.parse_args(argv)

    if args.embeddings and args.record_embeddings:
        parser.error("--embeddings (replay) and --record-embeddings are exclusive")

    configure_logging()
    settings = get_settings()
    corpus_dir = args.corpus or settings.corpus_dir
    database = build_database(settings)
    if database is None:
        print("DATABASE_URL is not configured", file=sys.stderr)
        return 2

    recorder: RecordingEmbedder | None = None
    try:
        embedder, recorder = choose_embedder(args, settings)
        if args.index == "hybrid" and embedder is None:
            print(
                "hybrid needs an embedder: configure EMBEDDING_BASE_URL and "
                "EMBEDDING_MODEL, or pass --embeddings with a recorded cache",
                file=sys.stderr,
            )
            return 2

        if args.seed or args.reindex:
            blobs = build_blob_store(settings)
            if blobs is None:
                print("BLOB_CONNECTION_STRING is not configured", file=sys.stderr)
                return 2
            service = IngestionService(database, blobs, embedder, settings)
            if args.seed:
                seed_corpus(service, corpus_dir)
            if args.reindex:
                for info in service.list_documents():
                    service.reindex(actor_id="evals", document_id=info.id)
            run_worker(service, database, settings, stop=threading.Event(), once=True)
            if not wait_for_idle_queue(database, args.wait_seconds):
                print(
                    "timed out waiting for indexing to finish (another worker "
                    "may be stuck, or a job is backing off)",
                    file=sys.stderr,
                )
                return 2
            failed = _failed_documents(service)
            if failed:
                print("indexing failed:", file=sys.stderr)
                for line in failed:
                    print(f"  {line}", file=sys.stderr)
                return 2

        try:
            acl = load_acl_oracle(corpus_dir)
            cases = load_golden(args.golden, acl)
        except GoldenError as exc:
            print(f"golden set error: {exc}", file=sys.stderr)
            return 2

        refs = document_refs(database)
        if not refs:
            print("no documents are indexed; run with --seed", file=sys.stderr)
            return 2

        options = _options(args, settings)
        try:
            report = run_eval(
                build_search(args.index, database, embedder, options),
                cases,
                refs=refs,
                acl=acl,
                limit=args.limit,
            )
            sweep_rows = (
                _sweep(
                    args.index,
                    database,
                    embedder,
                    options,
                    cases,
                    refs=refs,
                    acl=acl,
                    limit=args.limit,
                )
                if args.sweep
                else []
            )
        except MissingEmbeddingError as exc:
            print(f"embedding cache error: {exc}", file=sys.stderr)
            return 2
    finally:
        database.dispose()

    if recorder is not None and args.record_embeddings:
        recorder.cache.save(args.record_embeddings)
        print(f"recorded {len(recorder.cache)} embeddings -> {args.record_embeddings}")

    return _report(args, options, report, sweep_rows)


def _report(
    args: argparse.Namespace,
    options: SearchOptions,
    report: EvalReport,
    sweep_rows: list[dict[str, float]],
) -> int:
    print(
        f"index: {args.index}   limit: {args.limit} chunks/question   "
        f"min_similarity={options.min_similarity} min_lexical={options.min_lexical} "
        f"max_chunks_per_document={options.max_chunks_per_document}"
    )
    print(format_report(report, verbose=args.verbose))
    if sweep_rows:
        print("\nthreshold sweep:")
        print(_format_sweep(sweep_rows))

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "index": args.index,
                    "options": dataclasses.asdict(options),
                    "summary": report.summary,
                    "score_samples": report.score_samples,
                    "sweep": sweep_rows,
                    "failures": [
                        {"id": r.case.id, "persona": r.case.persona, "note": r.note}
                        for r in report.failures
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    exit_code = 0
    if report.summary["acl_leaks"] > 0:
        print("\nFAIL: ACL leaks found", file=sys.stderr)
        exit_code = 1
    if args.check:
        thresholds = json.loads(args.check.read_text(encoding="utf-8")).get(
            args.index, {}
        )
        problems = check_thresholds(report.summary, thresholds)
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        if problems:
            exit_code = 1
        else:
            print("\nthresholds: all met")
    return exit_code


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.WARNING)
    raise SystemExit(main())
