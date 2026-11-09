"""Import the synthetic corpus through the normal ingestion path.

``python -m leyline.seed [--corpus DIR] [--index]``

Each file starts with a header block (``Document Title:``, ``Document ID:``
and either ``Groups:`` or the legacy ``Access Level:``), followed by the
body. Re-running is safe: unchanged files are skipped, changed files become a
new version, and the ACL of an existing document is left alone so that edits
made in the admin UI are never overwritten.
"""

from __future__ import annotations

import argparse
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from leyline.config import get_settings
from leyline.ingestion.parsers import InvalidDocumentError
from leyline.ingestion.service import IngestionService
from leyline.services.document_loader import parse_document
from leyline.wiring import (
    build_blob_store,
    build_database,
    build_embedder,
    configure_logging,
)
from leyline.worker import run_worker

log = logging.getLogger("leyline.seed")

SEED_ACTOR = "seed"
# Legacy "Access Level:" values mapped onto group ACLs.
LEGACY_ACCESS_LEVELS: dict[str, list[str]] = {
    "general": ["*"],
    "restricted": ["security"],
}
SUFFIXES = {".txt", ".md"}


@dataclass
class SeedReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def groups_for(metadata: dict[str, object]) -> list[str] | None:
    """The ACL for a corpus file, or None when it declares none we understand."""
    explicit = metadata.get("groups")
    if isinstance(explicit, str):
        return [g.strip() for g in explicit.split(",") if g.strip()]
    level = metadata.get("access_level")
    if isinstance(level, str):
        return LEGACY_ACCESS_LEVELS.get(level.strip().lower())
    return None


def seed_corpus(service: IngestionService, corpus_dir: Path) -> SeedReport:
    report = SeedReport()
    for path in sorted(p for p in corpus_dir.rglob("*") if p.suffix in SUFFIXES):
        label = str(path.relative_to(corpus_dir))
        metadata = parse_document(path)
        title, ref, body = (
            metadata.get("title"),
            metadata.get("id"),
            metadata["content"],
        )
        groups = groups_for(metadata)
        if not (title and ref and body) or groups is None:
            log.warning("skipping %s: missing title, id, content or access", label)
            report.skipped.append(label)
            continue

        data = body.encode("utf-8")
        existing = service.find_by_source_ref(ref)
        try:
            if existing is None:
                service.submit_upload(
                    actor_id=SEED_ACTOR,
                    filename=path.name,
                    data=data,
                    title=title,
                    groups=groups,
                    source_ref=ref,
                )
                report.created.append(label)
            else:
                result = service.submit_upload(
                    actor_id=SEED_ACTOR,
                    filename=path.name,
                    data=data,
                    document_id=existing.id,
                )
                (report.unchanged if result.duplicate else report.updated).append(label)
        except InvalidDocumentError as exc:
            log.warning("skipping %s: %s", label, exc)
            report.skipped.append(label)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leyline.seed")
    parser.add_argument("--corpus", type=Path, default=None, help="corpus directory")
    parser.add_argument(
        "--index",
        action="store_true",
        help="also index the queued documents now (otherwise the worker does)",
    )
    args = parser.parse_args(argv)

    configure_logging()
    settings = get_settings()
    database = build_database(settings)
    blobs = build_blob_store(settings)
    if database is None or blobs is None:
        log.error("DATABASE_URL and BLOB_CONNECTION_STRING must be configured")
        return 2

    service = IngestionService(database, blobs, build_embedder(settings), settings)
    try:
        report = seed_corpus(service, args.corpus or settings.corpus_dir)
        log.info(
            "created=%d updated=%d unchanged=%d skipped=%d",
            len(report.created),
            len(report.updated),
            len(report.unchanged),
            len(report.skipped),
        )
        if args.index:
            done = run_worker(
                service, database, settings, stop=threading.Event(), once=True
            )
            log.info("indexed %d job(s)", done)
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
