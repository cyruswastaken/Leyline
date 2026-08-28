from flask import Flask, jsonify, request, render_template

from services.chat_service import process_message
from services.monitoring import (
    get_security_summary,
    get_recent_events
)
from services.document_loader import load_documents
from services.authorization import filter_authorized_documents
from services.retrieval import retrieve_documents


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

    # Temporary identity until authentication is implemented
    user_id = "local-user"

    # Temporary role until authentication is implemented
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


@app.get("/documents")
def get_documents():
    documents = load_documents()

    return jsonify(documents), 200


@app.get("/documents/<user_role>")
def get_authorized_documents(user_role):
    documents = load_documents()

    authorized_documents = filter_authorized_documents(
        documents,
        user_role
    )

    return jsonify(authorized_documents), 200


@app.get("/search/<user_role>")
def search_documents(user_role):

    query = request.args.get(
        "q",
        ""
    ).strip()

    if not query:
        return jsonify({
            "error": "Query parameter is required"
        }), 400

    documents = load_documents()

    authorized_documents = filter_authorized_documents(
        documents,
        user_role
    )

    results = retrieve_documents(
        query,
        authorized_documents
    )

    return jsonify(results), 200


if __name__ == "__main__":
    app.run(debug=True)