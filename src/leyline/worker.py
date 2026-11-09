"""Ingestion worker: ``python -m leyline.worker [--once]``.

Runs the same image as the web app. Any number of workers can run at once;
jobs are claimed with ``FOR UPDATE SKIP LOCKED`` and a crashed worker's job
is picked up again after its lease expires.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from pathlib import Path

from leyline.config import Settings, get_settings
from leyline.db.database import Database
from leyline.ingestion import jobs
from leyline.ingestion.service import IngestionService
from leyline.wiring import (
    build_blob_store,
    build_database,
    build_embedder,
    configure_logging,
)

log = logging.getLogger("leyline.worker")


def run_worker(
    service: IngestionService,
    database: Database,
    settings: Settings,
    *,
    stop: threading.Event,
    once: bool = False,
    heartbeat: Path | None = None,
) -> int:
    """Process jobs until stopped (or, with ``once``, until the queue is empty).

    Returns the number of jobs processed.
    """
    processed = 0
    while not stop.is_set():
        _beat(heartbeat)
        with database.session() as session:
            job = jobs.claim(session, lease_seconds=settings.ingest_lease_seconds)

        if job is None:
            if once:
                break
            stop.wait(settings.worker_poll_seconds)
            continue

        outcome = service.run_job(job)
        processed += 1
        log.info(
            "job=%s kind=%s document=%s attempt=%s/%s outcome=%s",
            job.id,
            job.kind,
            job.document_id,
            job.attempts,
            job.max_attempts,
            outcome,
        )
    return processed


def _beat(path: Path | None) -> None:
    """Record that the loop is alive. A failure here must never stop the worker."""
    if path is None:
        return
    try:
        path.touch()
    except OSError:
        log.warning("could not update heartbeat file %s", path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leyline.worker")
    parser.add_argument(
        "--once", action="store_true", help="drain the queue, then exit"
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

    stop = threading.Event()
    for signame in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, signame), lambda *_: stop.set())

    log.info(
        "worker started (embeddings: %s)",
        "on" if settings.embeddings_configured else "off",
    )
    try:
        run_worker(
            service,
            database,
            settings,
            stop=stop,
            once=args.once,
            heartbeat=settings.worker_heartbeat_path,
        )
    finally:
        database.dispose()
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
