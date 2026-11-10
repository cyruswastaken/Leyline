"""Ticket escalation: requesters and the support-agent queue.

Visibility is enforced by the provider (requester or support agent) as well as
by the decorators here, and an unknown id and someone else's id both answer 404.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime
from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask.typing import ResponseReturnValue

from leyline.auth.access import (
    current_principal,
    login_required,
    require_principal,
    require_roles,
)
from leyline.auth.principal import AppRole
from leyline.extensions import (
    app_settings,
    conversation_service,
    rate_limiter,
    ticket_provider,
)
from leyline.ports.tickets import (
    STATUSES,
    TicketInfo,
    TicketNotFoundError,
    TicketStateError,
)
from leyline.services.escalation import InvalidTicketError, build_draft, clean_text
from leyline.services.logger import log_security_event

bp = Blueprint("tickets", __name__)


def _info(ticket: TicketInfo) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                f.name: convert(getattr(value, f.name))
                for f in dataclasses.fields(value)
            }
        if isinstance(value, tuple | list):
            return [convert(v) for v in value]
        return value

    return convert(ticket)  # type: ignore[no-any-return]


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.errorhandler(InvalidTicketError)
def _invalid(exc: InvalidTicketError) -> ResponseReturnValue:
    return jsonify({"error": str(exc)}), 400


@bp.errorhandler(TicketNotFoundError)
def _not_found(_: TicketNotFoundError) -> ResponseReturnValue:
    return jsonify({"error": "Ticket not found"}), 404


@bp.errorhandler(TicketStateError)
def _state(exc: TicketStateError) -> ResponseReturnValue:
    return jsonify({"error": str(exc)}), 409


@bp.get("/tickets")
@login_required
def tickets_page() -> ResponseReturnValue:
    return render_template("tickets.html", user=current_principal())


@bp.post("/api/conversations/<uuid:conversation_id>/escalation-draft")
@login_required
def escalation_draft(conversation_id: uuid.UUID) -> ResponseReturnValue:
    """A prefilled ticket made of the user's own words; nothing is created."""
    principal = require_principal()
    draft = build_draft(
        conversation_service(), principal.user_id, conversation_id, app_settings()
    )
    if draft is None:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify({"subject": draft.subject, "description": draft.description})


@bp.post("/api/tickets")
@login_required
def create_ticket() -> ResponseReturnValue:
    settings = app_settings()
    principal = require_principal()
    data = _body()
    subject = clean_text(
        data.get("subject"), field="subject", limit=settings.ticket_max_subject
    )
    description = clean_text(
        data.get("description"),
        field="description",
        limit=settings.ticket_max_description,
    )
    if not rate_limiter().is_allowed(
        f"ticket:{principal.user_id}",
        limit=settings.ticket_rate_limit,
        window_seconds=settings.rate_limit_window_seconds,
    ):
        return jsonify({"error": "Too many tickets. Please try again later."}), 429

    ticket = ticket_provider().create(
        principal, subject=subject, description=description
    )
    log_security_event(
        event_type="TICKET_CREATED",
        user_id=principal.user_id,
        status_code=201,
        reason="ticket_created",
        details={"ticket_id": str(ticket.id)},
    )
    return jsonify(_info(ticket)), 201


@bp.get("/api/tickets")
@login_required
def my_tickets() -> ResponseReturnValue:
    principal = require_principal()
    return jsonify(
        {"tickets": [_info(t) for t in ticket_provider().list_mine(principal)]}
    )


@bp.get("/api/tickets/<uuid:ticket_id>")
@login_required
def get_ticket(ticket_id: uuid.UUID) -> ResponseReturnValue:
    return jsonify(_info(ticket_provider().get(require_principal(), ticket_id)))


@bp.post("/api/tickets/<uuid:ticket_id>/comments")
@login_required
def comment_on_ticket(ticket_id: uuid.UUID) -> ResponseReturnValue:
    principal = require_principal()
    body = clean_text(
        _body().get("body"), field="comment", limit=app_settings().ticket_max_comment
    )
    ticket = ticket_provider().comment(principal, ticket_id, body)
    return jsonify(_info(ticket))


@bp.post("/api/tickets/<uuid:ticket_id>/status")
@login_required
def change_status(ticket_id: uuid.UUID) -> ResponseReturnValue:
    """Agents set any status; the requester may close or reopen their own."""
    principal = require_principal()
    status = _body().get("status")
    if status not in STATUSES:
        return jsonify({"error": f"status must be one of {', '.join(STATUSES)}"}), 400
    ticket = ticket_provider().set_status(principal, ticket_id, status)
    log_security_event(
        event_type="TICKET_STATUS",
        user_id=principal.user_id,
        status_code=200,
        reason=status,
        details={"ticket_id": str(ticket_id)},
    )
    return jsonify(_info(ticket))


@bp.get("/api/agent/tickets")
@require_roles(AppRole.SUPPORT_AGENT)
def agent_queue() -> ResponseReturnValue:
    status = request.args.get("status") or None
    if status is not None and status not in STATUSES:
        return jsonify({"error": f"status must be one of {', '.join(STATUSES)}"}), 400
    tickets = ticket_provider().list_queue(require_principal(), status=status)
    return jsonify({"tickets": [_info(t) for t in tickets]})


@bp.post("/api/agent/tickets/<uuid:ticket_id>/assign")
@require_roles(AppRole.SUPPORT_AGENT)
def assign_ticket(ticket_id: uuid.UUID) -> ResponseReturnValue:
    principal = require_principal()
    to_self = _body().get("assign", True) is not False
    ticket = ticket_provider().assign(principal, ticket_id, to_self=to_self)
    return jsonify(_info(ticket))
