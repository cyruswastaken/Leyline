import os

from flask import Flask, jsonify, request, render_template

from services.chat_service import process_message
from services.monitoring import (
    get_security_summary,
    get_recent_events
)


MAX_MESSAGE_LENGTH = 2000


app = Flask(__name__)

# Reject oversized request bodies before they are parsed.
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "message": "Secure RAG Helpdesk API is running"
    }), 200


@app.post("/chat")
def chat():
    data = request.get_json(silent=True)

    if not isinstance(data, dict) or "message" not in data:
        return jsonify({
            "error": "Message is required"
        }), 400

    message = data["message"]

    if not isinstance(message, str):
        return jsonify({
            "error": "Message must be a string"
        }), 400

    message = message.strip()

    if not message:
        return jsonify({
            "error": "Message cannot be empty"
        }), 400

    if len(message) > MAX_MESSAGE_LENGTH:
        return jsonify({
            "error": (
                "Message is too long "
                f"(maximum {MAX_MESSAGE_LENGTH} characters)"
            )
        }), 400

    # Temporary identity until authentication is implemented (Phase 1).
    # Identity must never be taken from request input.
    user_id = "local-user"
    user_role = "employee"

    result = process_message(
        message,
        user_id,
        user_role
    )

    if not result["allowed"]:
        return jsonify({
            "error": result["error"],
            "reason": result["reason"]
        }), result["status_code"]

    return jsonify({
        "response": result["response"]
    }), result["status_code"]


# TODO(Phase 1): these routes expose operational data and must be
# restricted to an authenticated security_auditor before any shared use.

@app.get("/monitoring/summary")
def monitoring_summary():
    return jsonify(get_security_summary()), 200


@app.get("/monitoring/events")
def monitoring_events():
    return jsonify(get_recent_events()), 200


@app.get("/monitoring")
def monitoring_dashboard():
    return render_template("monitoring.html")


if __name__ == "__main__":
    # The debugger allows remote code execution; opt in explicitly.
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
