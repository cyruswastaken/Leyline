"""Persistent, owner-scoped conversations.

Every query filters on ``owner_id``. The owner always comes from the
authenticated principal; a conversation that exists but belongs to someone else
is indistinguishable from one that does not exist (callers answer 404).

Citations are stored as a snapshot (ids and titles), never as document text, and
are re-checked against the *current* ACLs each time a conversation is shown, so
a document the user can no longer read disappears from old answers too.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from leyline.auth.principal import Principal
from leyline.db.database import Database
from leyline.db.models import Conversation, Document, Feedback, Message
from leyline.services.authorization import readable_by

TITLE_LENGTH = 60
UNAVAILABLE_SOURCE = "Source no longer available"


@dataclass(frozen=True)
class ConversationSummary:
    id: uuid.UUID
    title: str
    updated_at: datetime


def _title_from(message: str) -> str:
    one_line = " ".join(message.split())
    return (
        one_line[: TITLE_LENGTH - 1] + "…" if len(one_line) > TITLE_LENGTH else one_line
    )


class ConversationService:
    def __init__(self, database: Database) -> None:
        self._db = database

    # --- writes ---------------------------------------------------------------

    def start(self, owner_id: str, first_message: str) -> uuid.UUID:
        conversation = Conversation(owner_id=owner_id, title=_title_from(first_message))
        with self._db.session() as session:
            session.add(conversation)
            session.flush()
            return conversation.id

    def owns(self, owner_id: str, conversation_id: uuid.UUID) -> bool:
        with self._db.session() as session:
            return (
                session.scalar(
                    select(Conversation.id).where(
                        Conversation.id == conversation_id,
                        Conversation.owner_id == owner_id,
                        Conversation.deleted_at.is_(None),
                    )
                )
                is not None
            )

    def add_message(
        self,
        owner_id: str,
        conversation_id: uuid.UUID,
        *,
        role: str,
        content: str,
        status: str = "complete",
        citations: list[dict[str, Any]] | None = None,
    ) -> uuid.UUID | None:
        """Append a message; None when the conversation is not the owner's."""
        with self._db.session() as session:
            # Lock the conversation row so concurrent turns get distinct numbers.
            locked = session.scalar(
                select(Conversation.id)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.owner_id == owner_id,
                    Conversation.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if locked is None:
                return None
            seq = (
                session.scalar(
                    select(func.coalesce(func.max(Message.seq), 0)).where(
                        Message.conversation_id == conversation_id
                    )
                )
                or 0
            ) + 1
            message = Message(
                conversation_id=conversation_id,
                seq=seq,
                role=role,
                content=content,
                status=status,
                citations=citations or [],
            )
            session.add(message)
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(updated_at=func.now())
            )
            session.flush()
            return message.id

    def delete(self, owner_id: str, conversation_id: uuid.UUID) -> bool:
        with self._db.session() as session:
            result = session.execute(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.owner_id == owner_id,
                    Conversation.deleted_at.is_(None),
                )
                .values(deleted_at=func.now())
            )
            return result.rowcount > 0  # type: ignore[attr-defined, no-any-return]

    def set_feedback(
        self,
        owner_id: str,
        message_id: uuid.UUID,
        *,
        rating: int,
        reason: str | None,
    ) -> bool:
        """Rate an assistant message in one of the owner's conversations."""
        with self._db.session() as session:
            found = session.scalar(
                select(Message.id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.id == message_id,
                    Message.role == "assistant",
                    Conversation.owner_id == owner_id,
                    Conversation.deleted_at.is_(None),
                )
            )
            if found is None:
                return False
            existing = session.scalar(
                select(Feedback).where(
                    Feedback.message_id == message_id, Feedback.user_id == owner_id
                )
            )
            if existing is None:
                session.add(
                    Feedback(
                        message_id=message_id,
                        user_id=owner_id,
                        rating=rating,
                        reason=reason,
                    )
                )
            else:
                existing.rating = rating
                existing.reason = reason
            return True

    # --- reads ----------------------------------------------------------------

    def list_for_owner(
        self, owner_id: str, *, limit: int = 50
    ) -> list[ConversationSummary]:
        with self._db.session() as session:
            rows = session.execute(
                select(Conversation.id, Conversation.title, Conversation.updated_at)
                .where(
                    Conversation.owner_id == owner_id,
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
            ).all()
        return [ConversationSummary(r.id, r.title, r.updated_at) for r in rows]

    def recent_user_messages(
        self, owner_id: str, conversation_id: uuid.UUID, *, limit: int
    ) -> list[str]:
        """The user's own earlier questions, oldest first.

        Assistant replies are deliberately not returned: they may contain text
        from documents the user could read *then* but not now, and nothing
        unauthorized may reach the model.
        """
        with self._db.session() as session:
            rows = session.scalars(
                select(Message.content)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role == "user",
                    Conversation.owner_id == owner_id,
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Message.seq.desc())
                .limit(limit)
            ).all()
        return list(reversed(rows))

    def feedback_reasons(self, owner_id: str, conversation_id: uuid.UUID) -> list[str]:
        """Reasons the owner gave when rating an answer down in this conversation."""
        with self._db.session() as session:
            rows = session.scalars(
                select(Feedback.reason)
                .join(Message, Message.id == Feedback.message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.conversation_id == conversation_id,
                    Conversation.owner_id == owner_id,
                    Conversation.deleted_at.is_(None),
                    Feedback.user_id == owner_id,
                    Feedback.rating == -1,
                    Feedback.reason.is_not(None),
                )
                .order_by(Feedback.created_at)
            ).all()
        return [r for r in rows if r]

    def get(
        self, principal: Principal, conversation_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """The conversation with citations re-authorized for ``principal``."""
        with self._db.session() as session:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.owner_id == principal.user_id,
                    Conversation.deleted_at.is_(None),
                )
            )
            if conversation is None:
                return None
            messages = session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq)
            ).all()
            feedback = {
                row.message_id: row
                for row in session.scalars(
                    select(Feedback).where(
                        Feedback.message_id.in_([m.id for m in messages]),
                        Feedback.user_id == principal.user_id,
                    )
                )
            }
            cited_ids = {
                uuid.UUID(c["document_id"])
                for m in messages
                for c in m.citations
                if c.get("document_id")
            }
            readable: set[uuid.UUID] = set()
            if cited_ids:
                readable = set(
                    session.scalars(
                        select(Document.id).where(
                            Document.id.in_(cited_ids),
                            Document.deleted_at.is_(None),
                            readable_by(principal),
                        )
                    )
                )
            return {
                "id": str(conversation.id),
                "title": conversation.title,
                "messages": [
                    {
                        "id": str(m.id),
                        "role": m.role,
                        "content": m.content,
                        "status": m.status,
                        "citations": [
                            _display_citation(c, readable) for c in m.citations
                        ],
                        "feedback": (
                            {
                                "rating": feedback[m.id].rating,
                                "reason": feedback[m.id].reason,
                            }
                            if m.id in feedback
                            else None
                        ),
                    }
                    for m in messages
                ],
            }

    # --- retention ------------------------------------------------------------

    def purge_older_than(self, days: int, *, now: datetime | None = None) -> int:
        """Hard-delete conversations last active more than ``days`` ago, plus
        any that users deleted that long ago. Returns how many were removed."""
        if days <= 0:
            return 0
        cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
        with self._db.session() as session:
            result = session.execute(
                delete(Conversation).where(Conversation.updated_at < cutoff)
            )
            return int(result.rowcount)  # type: ignore[attr-defined]


def _display_citation(
    citation: dict[str, Any], readable: set[uuid.UUID]
) -> dict[str, Any]:
    document_id = citation.get("document_id")
    allowed = bool(document_id) and uuid.UUID(document_id) in readable
    if not allowed:
        return {"n": citation.get("n"), "available": False, "title": UNAVAILABLE_SOURCE}
    return {
        "n": citation.get("n"),
        "available": True,
        "document_id": document_id,
        "title": citation.get("title"),
        "heading": citation.get("heading"),
        "cited": citation.get("cited", True),
    }
