from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue
from redis.exceptions import RedisError

from leyline.extensions import app_settings, redis_client

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def liveness() -> ResponseReturnValue:
    """Process is up. Must never depend on external services."""
    return jsonify({"status": "ok"}), 200


@bp.get("/readyz")
def readiness() -> ResponseReturnValue:
    """Dependencies required to serve requests are available."""
    settings = app_settings()

    try:
        redis_ok = bool(redis_client().ping())
    except RedisError:
        redis_ok = False

    checks = {
        "redis_reachable": redis_ok,
        "oidc_configured": settings.oidc_configured,
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
