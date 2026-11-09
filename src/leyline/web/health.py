from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue

from leyline.config import get_settings

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def liveness() -> ResponseReturnValue:
    """Process is up. Must never depend on external services."""
    return jsonify({"status": "ok"}), 200


@bp.get("/readyz")
def readiness() -> ResponseReturnValue:
    """Dependencies required to serve chat requests are available."""
    settings = get_settings()

    checks = {
        "llm_configured": settings.llm_configured,
        "corpus_available": settings.corpus_dir.is_dir(),
    }
    ready = all(checks.values())

    return jsonify(
        {
            "status": "ready" if ready else "not_ready",
            "checks": checks,
        }
    ), 200 if ready else 503
