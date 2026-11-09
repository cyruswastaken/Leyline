"""Document lifecycle: upload, index, re-version, re-index, ACL, delete.

All state lives in Postgres. Uploads and administrative changes are
synchronous and transactional; the slow work (parsing, embedding) happens in
the worker via the job queue.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, selectinload

from leyline.config import Settings
from leyline.db.database import Database
from leyline.db.models import (
    Chunk,
    Document,
    DocumentAcl,
    DocumentVersion,
    IngestionJob,
)
from leyline.ingestion import jobs
from leyline.ingestion.chunker import ChunkDraft, chunk_sections
from leyline.ingestion.parsers import (
    CONTENT_TYPES,
    InvalidDocumentError,
    Kind,
    Limits,
    detect_kind,
    parse,
)
from leyline.ingestion.validation import safe_filename, validate_groups, validate_title
from leyline.ports.blob import BlobNotFoundError, BlobStore
from leyline.ports.embeddings import Embedder
from leyline.services import audit

_KIND_BY_CONTENT_TYPE: dict[str, Kind] = {v: k for k, v in CONTENT_TYPES.items()}
_ACTIVE_VERSION_STATUSES = ("pending", "processing", "indexed")


class DocumentNotFoundError(LookupError):
    """No such (undeleted) document."""


class DocumentStateError(RuntimeError):
    """The requested action does not apply to the document's current state."""


@dataclass(frozen=True)
class SubmitResult:
    document_id: uuid.UUID
    version_id: uuid.UUID
    version: int
    duplicate: bool


@dataclass(frozen=True)
class JobInfo:
    state: str
    attempts: int
    max_attempts: int
    last_error: str | None


@dataclass(frozen=True)
class VersionInfo:
    id: uuid.UUID
    version: int
    filename: str
    size_bytes: int
    status: str
    error: str | None
    uploaded_by: str
    created_at: datetime
    indexed_at: datetime | None
    chunk_count: int
    embedding_model: str | None
    job: JobInfo | None


@dataclass(frozen=True)
class DocumentInfo:
    id: uuid.UUID
    title: str
    source_ref: str | None
    groups: list[str]
    created_by: str
    created_at: datetime
    updated_at: datetime
    current_version: VersionInfo | None
    latest_version: VersionInfo | None
    needs_reindex: bool
    versions: list[VersionInfo]


class IngestionService:
    def __init__(
        self,
        database: Database,
        blobs: BlobStore,
        embedder: Embedder | None,
        settings: Settings,
    ) -> None:
        self._db = database
        self._blobs = blobs
        self._embedder = embedder
        self._settings = settings
        self._limits = Limits(
            max_bytes=settings.max_upload_bytes,
            max_pdf_pages=settings.max_pdf_pages,
            max_docx_uncompressed_bytes=settings.max_docx_uncompressed_bytes,
        )

    # ------------------------------------------------------------------ #
    # Administrative operations (called from the web layer)
    # ------------------------------------------------------------------ #

    def submit_upload(
        self,
        *,
        actor_id: str,
        filename: str,
        data: bytes,
        title: str | None = None,
        groups: list[str] | None = None,
        document_id: uuid.UUID | None = None,
        source_ref: str | None = None,
    ) -> SubmitResult:
        """Store a file and queue it for indexing.

        With ``document_id`` this adds a new version to an existing document
        (its ACL is unchanged). Uploading the same bytes again as the latest
        version is a no-op and returns ``duplicate=True``.
        """
        kind = detect_kind(filename, data, self._limits)
        sha256 = hashlib.sha256(data).hexdigest()
        version_id = uuid.uuid4()

        new_title = validate_title(title or "") if document_id is None else None
        retitle = validate_title(title) if document_id is not None and title else None
        acl = validate_groups(groups or []) if document_id is None else frozenset()

        with self._db.session() as session:
            if document_id is None:
                document = Document(
                    id=uuid.uuid4(),
                    title=new_title or "",
                    source_ref=source_ref,
                    created_by=actor_id,
                )
                document.acl = [DocumentAcl(group_id=group) for group in sorted(acl)]
                session.add(document)
                number = 1
            else:
                document = self._lock_document(session, document_id)
                latest = session.scalars(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == document.id)
                    .order_by(DocumentVersion.version.desc())
                    .limit(1)
                ).first()
                if (
                    latest is not None
                    and latest.sha256 == sha256
                    and latest.status in _ACTIVE_VERSION_STATUSES
                ):
                    return SubmitResult(document.id, latest.id, latest.version, True)
                number = (latest.version if latest else 0) + 1
                if retitle:
                    document.title = retitle

            # Blob first: an orphaned blob after a failed commit is harmless,
            # a database row pointing at a missing blob is not.
            blob_key = f"documents/{document.id}/{version_id}"
            self._blobs.put(blob_key, data, CONTENT_TYPES[kind])

            session.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document.id,
                    version=number,
                    filename=safe_filename(filename),
                    content_type=CONTENT_TYPES[kind],
                    size_bytes=len(data),
                    sha256=sha256,
                    blob_key=blob_key,
                    status="pending",
                    uploaded_by=actor_id,
                )
            )
            session.flush()
            jobs.enqueue(
                session,
                kind="ingest",
                document_id=document.id,
                version_id=version_id,
                max_attempts=self._settings.ingest_max_attempts,
            )
            audit.record(
                session,
                actor_id=actor_id,
                action="document.upload" if number == 1 else "document.new_version",
                target_type="document",
                target_id=str(document.id),
                details={
                    "version": number,
                    "filename": safe_filename(filename),
                    "size_bytes": len(data),
                    "sha256": sha256,
                    **({"groups": sorted(acl)} if number == 1 else {}),
                },
            )
            return SubmitResult(document.id, version_id, number, False)

    def set_acl(
        self, *, actor_id: str, document_id: uuid.UUID, groups: list[str]
    ) -> frozenset[str]:
        """Replace the document's ACL. Takes effect on the next request."""
        acl = validate_groups(groups)
        with self._db.session() as session:
            document = self._lock_document(session, document_id)
            before = sorted(
                session.scalars(
                    select(DocumentAcl.group_id).where(
                        DocumentAcl.document_id == document.id
                    )
                )
            )
            session.execute(
                delete(DocumentAcl).where(DocumentAcl.document_id == document.id)
            )
            session.add_all(
                DocumentAcl(document_id=document.id, group_id=group)
                for group in sorted(acl)
            )
            document.updated_at = func.now()
            audit.record(
                session,
                actor_id=actor_id,
                action="document.acl_changed",
                target_type="document",
                target_id=str(document.id),
                details={"before": before, "after": sorted(acl)},
            )
        return acl

    def delete_document(self, *, actor_id: str, document_id: uuid.UUID) -> None:
        """Make the document unretrievable immediately; purge files later."""
        with self._db.session() as session:
            document = self._lock_document(session, document_id)
            document.deleted_at = func.now()
            document.current_version_id = None
            # Free the external id so the document can be re-imported later.
            document.source_ref = None
            session.execute(delete(Chunk).where(Chunk.document_id == document.id))
            session.execute(
                update(IngestionJob)
                .where(
                    IngestionJob.document_id == document.id,
                    IngestionJob.kind == "ingest",
                    IngestionJob.state == "queued",
                )
                .values(
                    state="failed",
                    last_error="document deleted",
                    finished_at=func.now(),
                )
            )
            jobs.enqueue(
                session,
                kind="purge",
                document_id=document.id,
                version_id=None,
                max_attempts=self._settings.ingest_max_attempts,
            )
            audit.record(
                session,
                actor_id=actor_id,
                action="document.delete",
                target_type="document",
                target_id=str(document.id),
                details={"title": document.title},
            )

    def reindex(self, *, actor_id: str, document_id: uuid.UUID) -> bool:
        """Queue the current (or, failing that, latest) version for re-indexing.

        Returns False if it was already queued. The old chunks keep serving
        until the new ones replace them atomically.
        """
        with self._db.session() as session:
            document = self._lock_document(session, document_id)
            version = (
                session.get(DocumentVersion, document.current_version_id)
                if document.current_version_id
                else session.scalars(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == document.id)
                    .order_by(DocumentVersion.version.desc())
                    .limit(1)
                ).first()
            )
            if version is None:
                raise DocumentStateError("The document has no uploaded version.")

            active = session.scalars(
                select(IngestionJob.id).where(
                    IngestionJob.version_id == version.id,
                    IngestionJob.kind == "ingest",
                    IngestionJob.state.in_(("queued", "running")),
                )
            ).first()
            if active is not None:
                return False

            if version.status == "failed":
                version.status = "pending"
            version.error = None
            jobs.enqueue(
                session,
                kind="ingest",
                document_id=document.id,
                version_id=version.id,
                max_attempts=self._settings.ingest_max_attempts,
            )
            audit.record(
                session,
                actor_id=actor_id,
                action="document.reindex",
                target_type="document",
                target_id=str(document.id),
                details={"version": version.version},
            )
            return True

    # ------------------------------------------------------------------ #
    # Read models for the admin UI
    # ------------------------------------------------------------------ #

    def list_documents(self) -> list[DocumentInfo]:
        with self._db.session() as session:
            documents = session.scalars(
                select(Document)
                .where(Document.deleted_at.is_(None))
                .options(selectinload(Document.acl), selectinload(Document.versions))
                .order_by(Document.title)
            ).all()
            return self._to_infos(session, documents)

    def get_document(self, document_id: uuid.UUID) -> DocumentInfo:
        with self._db.session() as session:
            document = session.scalars(
                select(Document)
                .where(Document.id == document_id, Document.deleted_at.is_(None))
                .options(selectinload(Document.acl), selectinload(Document.versions))
            ).first()
            if document is None:
                raise DocumentNotFoundError(str(document_id))
            return self._to_infos(session, [document])[0]

    def find_by_source_ref(self, source_ref: str) -> DocumentInfo | None:
        with self._db.session() as session:
            document = session.scalars(
                select(Document)
                .where(Document.source_ref == source_ref, Document.deleted_at.is_(None))
                .options(selectinload(Document.acl), selectinload(Document.versions))
            ).first()
            return self._to_infos(session, [document])[0] if document else None

    def _to_infos(
        self, session: Session, documents: Sequence[Document]
    ) -> list[DocumentInfo]:
        version_ids = [v.id for d in documents for v in d.versions]
        latest_job: dict[uuid.UUID, JobInfo] = {}
        if version_ids:
            rows = session.scalars(
                select(IngestionJob)
                .where(
                    IngestionJob.version_id.in_(version_ids),
                    IngestionJob.kind == "ingest",
                )
                .order_by(IngestionJob.created_at)
            )
            for job in rows:  # later rows overwrite earlier: newest job wins
                if job.version_id is None:
                    continue
                latest_job[job.version_id] = JobInfo(
                    job.state, job.attempts, job.max_attempts, job.last_error
                )

        infos: list[DocumentInfo] = []
        for document in documents:
            versions = [
                VersionInfo(
                    id=v.id,
                    version=v.version,
                    filename=v.filename,
                    size_bytes=v.size_bytes,
                    status=v.status,
                    error=v.error,
                    uploaded_by=v.uploaded_by,
                    created_at=v.created_at,
                    indexed_at=v.indexed_at,
                    chunk_count=v.chunk_count,
                    embedding_model=v.embedding_model,
                    job=latest_job.get(v.id),
                )
                for v in document.versions
            ]
            current = next(
                (v for v in versions if v.id == document.current_version_id), None
            )
            latest = versions[-1] if versions else None
            infos.append(
                DocumentInfo(
                    id=document.id,
                    title=document.title,
                    source_ref=document.source_ref,
                    groups=sorted(a.group_id for a in document.acl),
                    created_by=document.created_by,
                    created_at=document.created_at,
                    updated_at=document.updated_at,
                    current_version=current,
                    latest_version=latest,
                    needs_reindex=self._needs_reindex(current),
                    versions=versions,
                )
            )
        return infos

    def _needs_reindex(self, current: VersionInfo | None) -> bool:
        """Indexed with a different embedding model than the configured one."""
        if current is None or self._embedder is None:
            return False
        return current.embedding_model != self._embedder.model

    # ------------------------------------------------------------------ #
    # Worker: run one claimed job
    # ------------------------------------------------------------------ #

    def run_job(self, job: jobs.ClaimedJob) -> str:
        """Process a claimed job and record the outcome.

        Returns one of: ``indexed``, ``superseded``, ``skipped``, ``purged``,
        ``lost`` (lease taken over), ``retry`` (will be retried) or ``failed``.
        """
        try:
            if job.kind == "purge":
                return self._purge(job)
            return self._ingest(job)
        except InvalidDocumentError as exc:
            return self._record_failure(job, str(exc), retry=False)
        except BlobNotFoundError:
            return self._record_failure(job, "The stored file is missing.", retry=False)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            return self._record_failure(job, message, retry=True)

    def _ingest(self, job: jobs.ClaimedJob) -> str:
        if job.version_id is None:
            raise RuntimeError("ingest job has no version")
        with self._db.session() as session:
            document = session.get(Document, job.document_id)
            version = session.get(DocumentVersion, job.version_id)
            if document is None or version is None or document.deleted_at is not None:
                jobs.succeed(session, job)
                return "skipped"
            if not jobs.owns(session, job):
                return "lost"
            if version.id != document.current_version_id:
                version.status = "processing"
            version.error = None
            blob_key, sha256 = version.blob_key, version.sha256
            kind = _KIND_BY_CONTENT_TYPE[version.content_type]

        data = self._blobs.get(blob_key)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise InvalidDocumentError("The stored file failed its integrity check.")

        sections = parse(kind, data, self._limits)
        drafts = chunk_sections(
            sections,
            target_words=self._settings.chunk_target_words,
            overlap_words=self._settings.chunk_overlap_words,
        )
        if not drafts:
            raise InvalidDocumentError("The document contains no extractable text.")
        vectors = self._embed(drafts)

        with self._db.session() as session:
            if not jobs.owns(session, job):
                return "lost"
            document = self._lock_document_row(session, job.document_id)
            if document is None or document.deleted_at is not None:
                jobs.succeed(session, job)
                return "skipped"
            version = session.get(DocumentVersion, job.version_id)
            if version is None:
                raise RuntimeError("document version vanished during indexing")
            current = (
                session.get(DocumentVersion, document.current_version_id)
                if document.current_version_id
                else None
            )

            if (
                current is not None
                and current.id != version.id
                and current.version > version.version
            ):
                # A newer version went live while this one was processing.
                version.status = "superseded"
                jobs.succeed(session, job)
                return "superseded"

            # Replace the document's chunks in one transaction: searchers see
            # either the old set or the new set, never none and never both.
            session.execute(delete(Chunk).where(Chunk.document_id == document.id))
            session.add_all(
                Chunk(
                    document_id=document.id,
                    version_id=version.id,
                    chunk_index=draft.index,
                    heading=draft.heading,
                    content=draft.content,
                    embedding=vectors[i] if vectors else None,
                )
                for i, draft in enumerate(drafts)
            )
            if current is not None and current.id != version.id:
                current.status = "superseded"
            version.status = "indexed"
            version.error = None
            version.indexed_at = func.now()
            version.chunk_count = len(drafts)
            version.embedding_model = (
                self._embedder.model if vectors and self._embedder else None
            )
            version.embedding_dimensions = len(vectors[0]) if vectors else None
            document.current_version_id = version.id
            document.updated_at = func.now()
            jobs.succeed(session, job)
            return "indexed"

    def _embed(self, drafts: list[ChunkDraft]) -> list[list[float]] | None:
        if self._embedder is None:
            return None
        texts = [
            f"{d.heading}\n{d.content}" if d.heading else d.content for d in drafts
        ]
        size = self._settings.embedding_batch_size
        vectors: list[list[float]] = []
        for start in range(0, len(texts), size):
            vectors.extend(self._embedder.embed(texts[start : start + size]))
        if len(vectors) != len(texts):
            raise RuntimeError(
                "embedding provider returned the wrong number of vectors"
            )
        dimensions = {len(v) for v in vectors}
        if len(dimensions) != 1 or 0 in dimensions:
            raise RuntimeError("embedding provider returned inconsistent dimensions")
        if any(not math.isfinite(x) for v in vectors for x in v):
            raise RuntimeError("embedding provider returned non-finite values")
        return vectors

    def _purge(self, job: jobs.ClaimedJob) -> str:
        with self._db.session() as session:
            versions = session.scalars(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == job.document_id,
                    DocumentVersion.blob_deleted_at.is_(None),
                )
            ).all()
            for version in versions:
                self._blobs.delete(version.blob_key)
                version.blob_deleted_at = func.now()
            if not jobs.succeed(session, job):
                return "lost"
        return "purged"

    def _record_failure(
        self, job: jobs.ClaimedJob, message: str, *, retry: bool
    ) -> str:
        with self._db.session() as session:
            state = jobs.fail(
                session,
                job,
                error=message,
                retry=retry,
                backoff_seconds=self._settings.ingest_backoff_seconds,
            )
            if state == "lost":
                return "lost"
            if job.kind == "ingest" and job.version_id is not None:
                version = session.get(DocumentVersion, job.version_id)
                document = session.get(Document, job.document_id)
                if version is not None:
                    serving = (
                        document is not None
                        and document.current_version_id == version.id
                    )
                    version.error = message[:500]
                    if not serving:
                        version.status = "pending" if state == "queued" else "failed"
            return "retry" if state == "queued" else "failed"

    # ------------------------------------------------------------------ #

    @staticmethod
    def _lock_document_row(session: Session, document_id: uuid.UUID) -> Document | None:
        return session.scalars(
            select(Document).where(Document.id == document_id).with_for_update()
        ).first()

    def _lock_document(self, session: Session, document_id: uuid.UUID) -> Document:
        document = self._lock_document_row(session, document_id)
        if document is None or document.deleted_at is not None:
            raise DocumentNotFoundError(str(document_id))
        return document
