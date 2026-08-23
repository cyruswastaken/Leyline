from flask import Flask, jsonify, request, render_template

from services.chat_service import process_message


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

    response = process_message(message)

    return jsonify({
        "response": response
    }), 200


if __name__ == "__main__":
    app.run(debug=True)