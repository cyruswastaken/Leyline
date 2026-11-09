from collections.abc import Callable

from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue

from leyline.db import migrate
from leyline.extensions import (
    app_settings,
    blob_store,
    database,
    embedder,
    redis_client,
)

bp = Blueprint("health", __name__)


def _check(probe: Callable[[], bool]) -> bool:
    """Run a dependency probe; any failure (including 'not configured') is False."""
    try:
        return bool(probe())
    except Exception:
        return False


@bp.get("/healthz")
def liveness() -> ResponseReturnValue:
    """Process is up. Must never depend on external services."""
    return jsonify({"status": "ok"}), 200


@bp.get("/readyz")
def readiness() -> ResponseReturnValue:
    """Dependencies required to serve requests are available."""
    settings = app_settings()

    checks = {
        "redis_reachable": _check(lambda: redis_client().ping()),
        "database_reachable": _check(lambda: database().ping()),
        "schema_current": _check(lambda: migrate.is_current(database().engine)),
        "blob_storage_reachable": _check(lambda: blob_store().ping()),
        "oidc_configured": settings.oidc_configured,
        "llm_configured": settings.llm_configured,
    }
    ready = all(checks.values())

    return jsonify(
        {
            "status": "ready" if ready else "not_ready",
            "checks": checks,
            # Optional features: informational, never gate readiness.
            "optional": {"embeddings_configured": embedder() is not None},
        }
    ), 200 if ready else 503
