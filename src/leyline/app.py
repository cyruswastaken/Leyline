from flask import Flask

from leyline.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or get_settings()

    app = Flask(__name__)

    # Reject oversized request bodies before they are parsed.
    app.config["MAX_CONTENT_LENGTH"] = settings.max_request_bytes
    app.debug = settings.flask_debug

    from leyline.web import chat, health, monitoring

    app.register_blueprint(health.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(monitoring.bp)

    return app


if __name__ == "__main__":
    # Local development only. In containers the app runs under gunicorn.
    create_app().run()
