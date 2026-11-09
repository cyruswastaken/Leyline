"""A durable job queue in Postgres.

Jobs commit atomically with the document state that created them, so there
is no window where a document exists without its job (or the reverse). Workers
claim with ``FOR UPDATE SKIP LOCKED``, so any number of them can run without
double-processing, and a worker that dies mid-job is recovered when its lease
expires. A fencing token (``locked_by``) stops a worker that lost its lease
from completing a job another worker now owns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from leyline.db.models import IngestionJob

MAX_BACKOFF_SECONDS = 3600


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    kind: str
    document_id: uuid.UUID
    version_id: uuid.UUID | None
    attempts: int
    max_attempts: int
    token: str


def enqueue(
    session: Session,
    *,
    kind: str,
    document_id: uuid.UUID,
    version_id: uuid.UUID | None,
    max_attempts: int,
) -> IngestionJob:
    job = IngestionJob(
        kind=kind,
        document_id=document_id,
        version_id=version_id,
        state="queued",
        max_attempts=max_attempts,
    )
    session.add(job)
    return job


def claim(session: Session, *, lease_seconds: int) -> ClaimedJob | None:
    """Claim the next runnable job, or None. Uses database time throughout."""
    lease_expired = IngestionJob.locked_at < func.now() - timedelta(
        seconds=lease_seconds
    )
    candidate = session.execute(
        select(IngestionJob)
        .where(
            or_(
                and_(
                    IngestionJob.state == "queued", IngestionJob.run_after <= func.now()
                ),
                and_(IngestionJob.state == "running", lease_expired),
            )
        )
        .order_by(IngestionJob.run_after, IngestionJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if candidate is None:
        return None

    if candidate.attempts >= candidate.max_attempts:
        # A job that keeps killing its worker must not be retried forever.
        candidate.state = "failed"
        candidate.finished_at = func.now()
        candidate.last_error = "gave up: worker stopped repeatedly while processing"
        session.flush()
        return None

    token = uuid.uuid4().hex
    candidate.state = "running"
    candidate.attempts += 1
    candidate.locked_at = func.now()
    candidate.locked_by = token
    session.flush()
    return ClaimedJob(
        id=candidate.id,
        kind=candidate.kind,
        document_id=candidate.document_id,
        version_id=candidate.version_id,
        attempts=candidate.attempts,
        max_attempts=candidate.max_attempts,
        token=token,
    )


def owns(session: Session, job: ClaimedJob) -> bool:
    """True while this claim still holds the job (lease not taken over)."""
    return (
        session.execute(
            select(IngestionJob.id).where(
                IngestionJob.id == job.id, IngestionJob.locked_by == job.token
            )
        ).first()
        is not None
    )


def succeed(session: Session, job: ClaimedJob) -> bool:
    result = session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job.id, IngestionJob.locked_by == job.token)
        .values(
            state="succeeded",
            finished_at=func.now(),
            last_error=None,
            locked_at=None,
            locked_by=None,
        )
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def fail(
    session: Session,
    job: ClaimedJob,
    *,
    error: str,
    retry: bool,
    backoff_seconds: int,
) -> str:
    """Record a failure. Returns the new state: 'queued' (will retry) or 'failed'."""
    will_retry = retry and job.attempts < job.max_attempts
    delay = min(backoff_seconds * (2 ** (job.attempts - 1)), MAX_BACKOFF_SECONDS)
    values: dict[str, object] = {
        "last_error": error[:500],
        "locked_at": None,
        "locked_by": None,
    }
    if will_retry:
        values["state"] = "queued"
        values["run_after"] = func.now() + timedelta(seconds=delay)
    else:
        values["state"] = "failed"
        values["finished_at"] = func.now()
    result = session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job.id, IngestionJob.locked_by == job.token)
        .values(**values)
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        return "lost"
    return "queued" if will_retry else "failed"
