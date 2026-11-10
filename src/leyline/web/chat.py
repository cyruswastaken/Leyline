from __future__ import annotations

import json
import uuid
from collections.abc import Generator, Iterator
from typing import Any

from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from flask.typing import ResponseReturnValue

from leyline import telemetry
from leyline.auth.access import (
    current_principal,
    login_required,
    require_principal,
)
from leyline.extensions import (
    app_settings,
    chat_model,
    conversation_service,
    database,
    rate_limiter,
    search_index,
)
from leyline.services.chat_service import StreamingTurn, begin_turn, process_message

bp = Blueprint("chat", __name__)

MAX_FEEDBACK_REASON = 500


@bp.get("/")
@login_required
def index() -> ResponseReturnValue:
    return render_template("index.html", user=current_principal())


class _BadRequest(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


@bp.errorhandler(_BadRequest)
def _bad_request(exc: _BadRequest) -> ResponseReturnValue:
    return jsonify({"error": exc.error}), 400


def _read_message() -> str:
    """The validated message from the JSON body (raises ``_BadRequest``)."""
    settings = app_settings()
    data = request.get_json(silent=True)

    if not isinstance(data, dict) or "message" not in data:
        raise _BadRequest("Message is required")

    message = data["message"]
    if not isinstance(message, str):
        raise _BadRequest("Message must be a string")

    message = message.strip()
    if not message:
        raise _BadRequest("Message cannot be empty")

    max_length = settings.max_message_length
    if len(message) > max_length:
        raise _BadRequest(f"Message is too long (maximum {max_length} characters)")
    return message


@bp.post("/chat")
@login_required
def chat() -> ResponseReturnValue:
    """One stateless question and a complete JSON answer."""
    settings = app_settings()
    principal = require_principal()

    message = _read_message()

    # Identity is the verified principal from the server-side session,
    # never anything supplied in the request.
    result = process_message(
        message,
        principal,
        rate_limiter=rate_limiter(),
        settings=settings,
        search=search_index(),
        database=database(),
        model=chat_model(),
    )

    if not result["allowed"]:
        return jsonify({"error": result["error"], "reason": result["reason"]}), result[
            "status_code"
        ]

    return jsonify(
        {"response": result["response"], "citations": result.get("citations", [])}
    ), result["status_code"]


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def _parse_id(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@bp.post("/api/chat/stream")
@login_required
def chat_stream() -> ResponseReturnValue:
    """A conversation turn, streamed as server-sent events.

    Body: ``{"message": str, "conversation_id": str | null}``. Events:
    ``meta`` -> ``delta``* -> ``done`` | ``error``. Closing the connection
    cancels generation; what was produced so far is kept, marked cancelled.
    """
    settings = app_settings()
    principal = require_principal()

    message = _read_message()

    conversation_id = None
    raw_id = (request.get_json(silent=True) or {}).get("conversation_id")
    if raw_id is not None:
        conversation_id = _parse_id(raw_id)
        if conversation_id is None:
            return jsonify(
                {"error": "Conversation not found", "reason": "conversation_not_found"}
            ), 404

    turn = begin_turn(
        message,
        principal,
        conversation_id,
        rate_limiter=rate_limiter(),
        settings=settings,
        search=search_index(),
        database=database(),
        model=chat_model(),
        conversations=conversation_service(),
    )
    if not isinstance(turn, StreamingTurn):
        return jsonify({"error": turn["error"], "reason": turn["reason"]}), turn[
            "status_code"
        ]

    def generate() -> Iterator[str]:
        events: Generator[dict[str, Any], None, None] = turn.events()
        try:
            for event in events:
                yield _sse(event)
        finally:
            events.close()

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"  # keep proxies from buffering
    return response


@bp.get("/api/conversations")
@login_required
def list_conversations() -> ResponseReturnValue:
    principal = require_principal()
    summaries = conversation_service().list_for_owner(principal.user_id)
    return jsonify(
        {
            "conversations": [
                {
                    "id": str(s.id),
                    "title": s.title,
                    "updated_at": s.updated_at.isoformat(),
                }
                for s in summaries
            ]
        }
    )


@bp.get("/api/conversations/<uuid:conversation_id>")
@login_required
def get_conversation(conversation_id: uuid.UUID) -> ResponseReturnValue:
    principal = require_principal()
    conversation = conversation_service().get(principal, conversation_id)
    if conversation is None:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify(conversation)


@bp.delete("/api/conversations/<uuid:conversation_id>")
@login_required
def delete_conversation(conversation_id: uuid.UUID) -> ResponseReturnValue:
    principal = require_principal()
    if not conversation_service().delete(principal.user_id, conversation_id):
        return jsonify({"error": "Conversation not found"}), 404
    return "", 204


@bp.post("/api/messages/<uuid:message_id>/feedback")
@login_required
def message_feedback(message_id: uuid.UUID) -> ResponseReturnValue:
    principal = require_principal()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON body is required"}), 400

    rating = data.get("rating")
    if isinstance(rating, bool) or rating not in (1, -1):
        return jsonify({"error": "rating must be 1 or -1"}), 400

    reason = data.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            return jsonify({"error": "reason must be a string"}), 400
        reason = reason.strip()[:MAX_FEEDBACK_REASON] or None

    if not conversation_service().set_feedback(
        principal.user_id, message_id, rating=rating, reason=reason
    ):
        return jsonify({"error": "Message not found"}), 404
    telemetry.feedback(rating)
    return jsonify({"ok": True})
