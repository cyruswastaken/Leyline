"""Turning a conversation into a support ticket, without leaking context.

A ticket is read by support agents, who may not be allowed to read the
documents the assistant used. So the draft is built from the requester's *own*
words only: their questions and the reasons they gave when rating an answer
down. It never contains assistant text, retrieved text or source titles. The
user reviews and may edit the draft before anything is created, and whatever
they submit is scrubbed of credential-shaped strings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from leyline.config import Settings
from leyline.services.conversations import ConversationService
from leyline.services.guardrails import redact


class InvalidTicketError(ValueError):
    """The submitted text is missing or too long."""


@dataclass(frozen=True)
class Draft:
    subject: str
    description: str


def build_draft(
    conversations: ConversationService,
    owner_id: str,
    conversation_id: uuid.UUID,
    settings: Settings,
) -> Draft | None:
    """None when the conversation is not the owner's (or does not exist)."""
    if not conversations.owns(owner_id, conversation_id):
        return None
    questions = conversations.recent_user_messages(owner_id, conversation_id, limit=10)
    reasons = conversations.feedback_reasons(owner_id, conversation_id)
    if not questions:
        return Draft("", "")

    subject = " ".join(questions[0].split())
    if len(subject) > settings.ticket_max_subject:
        subject = subject[: settings.ticket_max_subject - 1] + "…"

    lines = [
        "The helpdesk assistant could not resolve my question.",
        "",
        "What I asked:",
    ]
    lines += [f"- {q}" for q in questions]
    if reasons:
        lines += ["", "Why the answer did not help:"]
        lines += [f"- {r}" for r in reasons]
    lines += ["", "Additional details:", ""]
    description = redact("\n".join(lines))
    return Draft(redact(subject), description[: settings.ticket_max_description])


def clean_text(value: object, *, field: str, limit: int, required: bool = True) -> str:
    """Validate user-submitted ticket text and scrub credentials from it."""
    if not isinstance(value, str):
        raise InvalidTicketError(f"{field} must be text")
    raw = value.strip()
    if required and not raw:
        raise InvalidTicketError(f"{field} cannot be empty")
    # Measured before scrubbing, so redaction cannot be used to smuggle in more.
    if len(raw) > limit:
        raise InvalidTicketError(f"{field} is too long (maximum {limit} characters)")
    return redact(raw)
