from flask import Blueprint, jsonify, render_template, request
from flask.typing import ResponseReturnValue

from leyline.config import get_settings
from leyline.services.chat_service import process_message

bp = Blueprint("chat", __name__)


@bp.get("/")
def index() -> ResponseReturnValue:
    return render_template("index.html")


@bp.post("/chat")
def chat() -> ResponseReturnValue:
    max_length = get_settings().max_message_length

    data = request.get_json(silent=True)

    if not isinstance(data, dict) or "message" not in data:
        return jsonify({"error": "Message is required"}), 400

    message = data["message"]

    if not isinstance(message, str):
        return jsonify({"error": "Message must be a string"}), 400

    message = message.strip()

    if not message:
        return jsonify({"error": "Message cannot be empty"}), 400

    if len(message) > max_length:
        return jsonify(
            {"error": (f"Message is too long (maximum {max_length} characters)")}
        ), 400

    # Temporary identity until authentication is implemented (Phase 1).
    # Identity must never be taken from request input.
    user_id = "local-user"
    user_role = "employee"

    result = process_message(message, user_id, user_role)

    if not result["allowed"]:
        return jsonify(
            {
                "error": result["error"],
                "reason": result["reason"],
            }
        ), result["status_code"]

    return jsonify({"response": result["response"]}), result["status_code"]
