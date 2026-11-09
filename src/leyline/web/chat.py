from flask import Blueprint, jsonify, render_template, request
from flask.typing import ResponseReturnValue

from leyline.auth.access import (
    current_principal,
    login_required,
    require_principal,
)
from leyline.extensions import app_settings, database, rate_limiter, search_index
from leyline.services.chat_service import process_message

bp = Blueprint("chat", __name__)


@bp.get("/")
@login_required
def index() -> ResponseReturnValue:
    return render_template("index.html", user=current_principal())


@bp.post("/chat")
@login_required
def chat() -> ResponseReturnValue:
    settings = app_settings()
    principal = require_principal()

    data = request.get_json(silent=True)

    if not isinstance(data, dict) or "message" not in data:
        return jsonify({"error": "Message is required"}), 400

    message = data["message"]

    if not isinstance(message, str):
        return jsonify({"error": "Message must be a string"}), 400

    message = message.strip()

    if not message:
        return jsonify({"error": "Message cannot be empty"}), 400

    max_length = settings.max_message_length
    if len(message) > max_length:
        return jsonify(
            {"error": (f"Message is too long (maximum {max_length} characters)")}
        ), 400

    # Identity is the verified principal from the server-side session,
    # never anything supplied in the request.
    result = process_message(
        message,
        principal,
        rate_limiter=rate_limiter(),
        settings=settings,
        search=search_index(),
        database=database(),
    )

    if not result["allowed"]:
        return jsonify(
            {
                "error": result["error"],
                "reason": result["reason"],
            }
        ), result["status_code"]

    return jsonify({"response": result["response"]}), result["status_code"]
