from flask import Flask, jsonify, request, render_template

from services.chat_service import process_message

from services.monitoring import get_security_summary

from services.monitoring import (
    get_security_summary,
    get_recent_events
)

app = Flask(__name__)


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
    data = request.get_json()

    if not data or "message" not in data:
        return jsonify({
            "error": "Message is required"
        }), 400

    message = data["message"].strip()

    if not message:
        return jsonify({
            "error": "Message cannot be empty"
        }), 400

    # Temporary identity until Entra ID authentication is added
    user_id = "local-user"

    result = process_message(message, user_id)

    if not result["allowed"]:
        return jsonify({
            "error": result["error"],
            "reason": result["reason"]
        }), result["status_code"]

    return jsonify({
        "response": result["response"]
    }), result["status_code"]


@app.get("/monitoring/summary")
def monitoring_summary():
    summary = get_security_summary()

    return jsonify(summary), 200

@app.get("/monitoring/events")
def monitoring_events():
    events = get_recent_events()

    return jsonify(events), 200

@app.get("/monitoring")
def monitoring_dashboard():
    return render_template("monitoring.html")

if __name__ == "__main__":
    app.run(debug=True)