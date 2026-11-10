"""The built-in ticket queue, stored in Postgres.

Visibility is decided here as well as in the routes: a ticket is visible to its
requester and to support agents, nobody else. Anything else is reported as
"not found", so ticket ids cannot be probed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leyline.auth.principal import AppRole, Principal
from leyline.db.database import Database
from leyline.db.models import Ticket, TicketEvent
from leyline.ports.tickets import (
    STATUSES,
    TicketEventInfo,
    TicketInfo,
    TicketNotFoundError,
    TicketStateError,
)


def _name(principal: Principal) -> str:
    return principal.name or principal.email or principal.user_id


def _is_agent(principal: Principal) -> bool:
    return principal.has_role(AppRole.SUPPORT_AGENT)


def _summary(ticket: Ticket) -> TicketInfo:
    return TicketInfo(
        id=ticket.id,
        subject=ticket.subject,
        status=ticket.status,
        requester_name=ticket.requester_name,
        assignee_name=ticket.assignee_name,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


def _detail(ticket: Ticket) -> TicketInfo:
    return TicketInfo(
        id=ticket.id,
        subject=ticket.subject,
        status=ticket.status,
        requester_name=ticket.requester_name,
        assignee_name=ticket.assignee_name,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        description=ticket.description,
        events=tuple(
            TicketEventInfo(
                kind=e.kind,
                actor_name=e.actor_name,
                body=e.body,
                created_at=e.created_at,
                by_requester=e.actor_id == ticket.requester_id,
            )
            for e in ticket.events
        ),
    )


class PostgresTicketProvider:
    def __init__(self, database: Database) -> None:
        self._db = database

    # --- helpers -----------------------------------------------------------

    def _load(
        self, session: Session, caller: Principal, ticket_id: uuid.UUID
    ) -> Ticket:
        """The ticket, locked for update, if ``caller`` may see it."""
        ticket = session.scalar(
            select(Ticket).where(Ticket.id == ticket_id).with_for_update()
        )
        if ticket is None or not (
            ticket.requester_id == caller.user_id or _is_agent(caller)
        ):
            raise TicketNotFoundError(str(ticket_id))
        return ticket

    @staticmethod
    def _event(
        session: Session, ticket: Ticket, actor: Principal, kind: str, body: str
    ) -> None:
        session.add(
            TicketEvent(
                ticket_id=ticket.id,
                kind=kind,
                actor_id=actor.user_id,
                actor_name=_name(actor),
                body=body,
            )
        )
        ticket.updated_at = func.now()

    # --- operations ---------------------------------------------------------

    def create(
        self, requester: Principal, *, subject: str, description: str
    ) -> TicketInfo:
        with self._db.session() as session:
            ticket = Ticket(
                requester_id=requester.user_id,
                requester_name=_name(requester),
                subject=subject,
                description=description,
            )
            session.add(ticket)
            session.flush()
            self._event(session, ticket, requester, "created", "")
            session.flush()
            session.refresh(ticket)
            return _detail(ticket)

    def get(self, caller: Principal, ticket_id: uuid.UUID) -> TicketInfo:
        with self._db.session() as session:
            return _detail(self._load(session, caller, ticket_id))

    def list_mine(self, requester: Principal, *, limit: int = 50) -> list[TicketInfo]:
        with self._db.session() as session:
            rows = session.scalars(
                select(Ticket)
                .where(Ticket.requester_id == requester.user_id)
                .order_by(Ticket.updated_at.desc())
                .limit(limit)
            ).all()
            return [_summary(t) for t in rows]

    def list_queue(
        self, agent: Principal, *, status: str | None = None, limit: int = 100
    ) -> list[TicketInfo]:
        if not _is_agent(agent):
            raise TicketStateError("support agents only")
        if status is not None and status not in STATUSES:
            raise TicketStateError(f"unknown status {status!r}")
        with self._db.session() as session:
            query = select(Ticket).order_by(Ticket.updated_at.desc()).limit(limit)
            if status is not None:
                query = query.where(Ticket.status == status)
            return [_summary(t) for t in session.scalars(query).all()]

    def comment(self, caller: Principal, ticket_id: uuid.UUID, body: str) -> TicketInfo:
        with self._db.session() as session:
            ticket = self._load(session, caller, ticket_id)
            if ticket.status == "closed":
                raise TicketStateError("this ticket is closed")
            self._event(session, ticket, caller, "comment", body)
            session.flush()
            session.refresh(ticket)
            return _detail(ticket)

    def set_status(
        self, caller: Principal, ticket_id: uuid.UUID, status: str
    ) -> TicketInfo:
        if status not in STATUSES:
            raise TicketStateError(f"unknown status {status!r}")
        with self._db.session() as session:
            ticket = self._load(session, caller, ticket_id)
            if not _is_agent(caller):
                # A requester may close their own ticket, or reopen a resolved one.
                reopening = status == "open" and ticket.status == "resolved"
                if status != "closed" and not reopening:
                    raise TicketStateError("you may only close or reopen your ticket")
            if ticket.status == status:
                return _detail(ticket)
            previous = ticket.status
            ticket.status = status
            ticket.resolved_at = func.now() if status == "resolved" else None
            self._event(
                session, ticket, caller, "status_changed", f"{previous} -> {status}"
            )
            session.flush()
            session.refresh(ticket)
            return _detail(ticket)

    def assign(
        self, agent: Principal, ticket_id: uuid.UUID, *, to_self: bool
    ) -> TicketInfo:
        if not _is_agent(agent):
            raise TicketStateError("support agents only")
        with self._db.session() as session:
            ticket = self._load(session, agent, ticket_id)
            if to_self:
                ticket.assignee_id, ticket.assignee_name = agent.user_id, _name(agent)
                if ticket.status == "open":
                    ticket.status = "in_progress"
                body = f"assigned to {_name(agent)}"
            else:
                ticket.assignee_id = ticket.assignee_name = None
                body = "unassigned"
            self._event(session, ticket, agent, "assigned", body)
            session.flush()
            session.refresh(ticket)
            return _detail(ticket)
