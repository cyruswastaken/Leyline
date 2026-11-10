from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from leyline.auth.principal import Principal

STATUSES = ("open", "in_progress", "resolved", "closed")


class TicketNotFoundError(LookupError):
    """No such ticket, or the caller may not see it (indistinguishable)."""


class TicketStateError(ValueError):
    """The requested change is not allowed for this caller or state."""


@dataclass(frozen=True)
class TicketEventInfo:
    kind: str
    actor_name: str
    body: str
    created_at: datetime
    # True when the actor is the requester (lets the UI label "You" / "Support").
    by_requester: bool


@dataclass(frozen=True)
class TicketInfo:
    id: uuid.UUID
    subject: str
    status: str
    requester_name: str
    assignee_name: str | None
    created_at: datetime
    updated_at: datetime
    description: str = ""
    events: tuple[TicketEventInfo, ...] = field(default_factory=tuple)


class TicketProvider(Protocol):
    """Where tickets live. The built-in queue is the default; ServiceNow or
    Jira would be further implementations. The caller is always a verified
    ``Principal``; implementations decide what that principal may see."""

    def create(
        self, requester: Principal, *, subject: str, description: str
    ) -> TicketInfo: ...

    def get(self, caller: Principal, ticket_id: uuid.UUID) -> TicketInfo:
        """Requester or support agent only; otherwise ``TicketNotFoundError``."""
        ...

    def list_mine(
        self, requester: Principal, *, limit: int = 50
    ) -> list[TicketInfo]: ...

    def list_queue(
        self, agent: Principal, *, status: str | None = None, limit: int = 100
    ) -> list[TicketInfo]:
        """The support queue. Agents only."""
        ...

    def comment(
        self, caller: Principal, ticket_id: uuid.UUID, body: str
    ) -> TicketInfo: ...

    def set_status(
        self, caller: Principal, ticket_id: uuid.UUID, status: str
    ) -> TicketInfo: ...

    def assign(
        self, agent: Principal, ticket_id: uuid.UUID, *, to_self: bool
    ) -> TicketInfo: ...
