"""Relational schema.

The database is the single source of truth for documents and who may read
them. ``document_acl`` is normalized (one row per document and group) and is
queried at search time, so changing an ACL or deleting a document takes
effect on the very next request.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from leyline.constants import EMBEDDING_DIMENSIONS

# Reserved ACL entry meaning "any signed-in user". Only knowledge-base admins
# can set it, and every change is audited.
EVERYONE_GROUP = "*"

VERSION_STATUSES = ("pending", "processing", "indexed", "failed", "superseded")
JOB_KINDS = ("ingest", "purge")
JOB_STATES = ("queued", "running", "succeeded", "failed")
MESSAGE_ROLES = ("user", "assistant")
MESSAGE_STATUSES = ("complete", "error", "cancelled")

TSV_EXPRESSION = (
    "to_tsvector('english'::regconfig, coalesce(heading, '') || ' ' || content)"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text)
    # Stable external identifier (e.g. the legacy corpus id) so seeding is
    # idempotent. Null for documents uploaded through the admin UI.
    source_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "document_versions.id",
            name="fk_documents_current_version",
            use_alter=True,
            ondelete="SET NULL",
        )
    )
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document",
        foreign_keys="DocumentVersion.document_id",
        order_by="DocumentVersion.version",
    )
    acl: Mapped[list[DocumentAcl]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_versions_number"),
        CheckConstraint(
            _in("status", VERSION_STATUSES), name="ck_document_versions_status"
        ),
        Index("ix_document_versions_document_sha", "document_id", "sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    # Display only: never used to build a storage path.
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    blob_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Recorded so a change of embedding model is detected and forces a re-index.
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    blob_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[Document] = relationship(
        back_populates="versions", foreign_keys=[document_id]
    )


class DocumentAcl(Base):
    __tablename__ = "document_acl"
    __table_args__ = (
        CheckConstraint("length(group_id) > 0", name="ck_document_acl_group_not_empty"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(Text, primary_key=True)

    document: Mapped[Document] = relationship(back_populates="acl")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "chunk_index", name="uq_chunks_version_index"),
        Index("ix_chunks_document_id", "document_id"),
        Index("ix_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    content_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed(TSV_EXPRESSION, persisted=True)
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(_in("kind", JOB_KINDS), name="ck_ingestion_jobs_kind"),
        CheckConstraint(_in("state", JOB_STATES), name="ck_ingestion_jobs_state"),
        Index("ix_ingestion_jobs_claim", "state", "run_after"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16))
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    state: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fencing token: only the claim that holds this value may finish the job.
    locked_by: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Administrative actions (who changed which document or ACL)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_ts", "ts"),
        Index("ix_audit_events_actor", "actor_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actor_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )


class Conversation(Base):
    """A chat thread. Owner-scoped: every query filters on ``owner_id``, which
    always comes from the authenticated principal."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.seq", cascade="all, delete"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_sequence"),
        CheckConstraint(_in("role", MESSAGE_ROLES), name="ck_messages_role"),
        CheckConstraint(_in("status", MESSAGE_STATUSES), name="ck_messages_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="complete")
    # [{"document_id", "title", "heading", "chunk_id", "n"}]: a snapshot of what
    # the answer was based on. Readability is re-checked every time it is shown.
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_feedback_message_user"),
        CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    user_id: Mapped[str] = mapped_column(Text)
    rating: Mapped[int] = mapped_column(SmallInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


TICKET_STATUSES = ("open", "in_progress", "resolved", "closed")
TICKET_EVENT_KINDS = ("created", "comment", "status_changed", "assigned")


class Ticket(Base):
    """A support request. Holds only what the requester approved: it has no
    link to the conversation, the sources or the assistant's text."""

    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(_in("status", TICKET_STATUSES), name="ck_tickets_status"),
        Index("ix_tickets_requester_updated", "requester_id", "updated_at"),
        Index("ix_tickets_status_updated", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    requester_id: Mapped[str] = mapped_column(Text)
    requester_name: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    assignee_id: Mapped[str | None] = mapped_column(Text)
    assignee_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list[TicketEvent]] = relationship(
        back_populates="ticket", order_by="TicketEvent.id", cascade="all, delete"
    )


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (
        CheckConstraint(_in("kind", TICKET_EVENT_KINDS), name="ck_ticket_events_kind"),
        Index("ix_ticket_events_ticket", "ticket_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(Text)
    actor_name: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped[Ticket] = relationship(back_populates="events")


class SecurityEvent(Base):
    """Every logged security or system event (logins, denials, retrievals, model
    errors, ticket actions). Append-only: a trigger rejects UPDATE and DELETE,
    and the application's database role should hold INSERT and SELECT only."""

    __tablename__ = "security_events"
    __table_args__ = (
        Index("ix_security_events_ts", "ts"),
        Index("ix_security_events_type_ts", "event_type", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    event_type: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
