from __future__ import annotations

from flask import Flask, g, jsonify, request, session
from flask.typing import ResponseReturnValue
from redis import Redis
from redis.exceptions import RedisError
from werkzeug.middleware.proxy_fix import ProxyFix

from leyline.auth.access import current_principal, load_principal
from leyline.auth.csrf import SAFE_METHODS, csrf_failure_reason
from leyline.auth.oidc import init_oidc
from leyline.auth.sessions import RedisSessionInterface, ServerSession
from leyline.config import Settings, get_settings
from leyline.db.database import Database
from leyline.extensions import (
    BLOB_KEY,
    DATABASE_KEY,
    EMBEDDER_KEY,
    INGESTION_KEY,
    RATE_LIMITER_KEY,
    REDIS_KEY,
    SEARCH_KEY,
    SETTINGS_KEY,
    NotConfiguredError,
)
from leyline.ingestion.service import IngestionService
from leyline.ports.blob import BlobStore
from leyline.ports.embeddings import Embedder
from leyline.ports.search import SearchIndex
from leyline.ratelimit import RedisRateLimiter
from leyline.security_headers import init_security_headers
from leyline.services.logger import log_security_event
from leyline.wiring import (
    build_blob_store,
    build_database,
    build_embedder,
    build_search_index,
)

# Probes must keep working under load and without a session store.
_RATE_LIMIT_EXEMPT_ENDPOINTS = frozenset({"health.liveness", "health.readiness"})
_STATELESS_PATHS = frozenset({"/healthz", "/readyz"})


def create_app(
    settings: Settings | None = None,
    *,
    redis_client: Redis | None = None,
    database: Database | None = None,
    blob_store: BlobStore | None = None,
    embedder: Embedder | None = None,
    search_index: SearchIndex | None = None,
) -> Flask:
    """Build the application. Collaborators default to ones built from
    settings; tests inject their own."""
    settings = settings or get_settings()

    app = Flask(__name__)

    # Reject oversized request bodies before they are parsed.
    app.config["MAX_CONTENT_LENGTH"] = settings.max_request_bytes
    app.debug = settings.flask_debug

    if settings.trusted_proxy_count:
        count = settings.trusted_proxy_count
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=count, x_proto=count, x_host=count
        )

    client = redis_client or Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )
    limiter = RedisRateLimiter(client)

    app.extensions[SETTINGS_KEY] = settings
    app.extensions[REDIS_KEY] = client
    app.extensions[RATE_LIMITER_KEY] = limiter

    database = database or build_database(settings)
    blob_store = blob_store or build_blob_store(settings)
    embedder = embedder or build_embedder(settings)
    if search_index is None and database is not None:
        search_index = build_search_index(settings, database, embedder)
    app.extensions[DATABASE_KEY] = database
    app.extensions[BLOB_KEY] = blob_store
    app.extensions[EMBEDDER_KEY] = embedder
    app.extensions[SEARCH_KEY] = search_index
    app.extensions[INGESTION_KEY] = (
        IngestionService(database, blob_store, embedder, settings)
        if database is not None and blob_store is not None
        else None
    )

    app.session_interface = RedisSessionInterface(
        client,
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
        secure=settings.session_cookie_secure,
        stateless_paths=_STATELESS_PATHS,
    )

    init_oidc(app, settings)
    init_security_headers(app, settings)

    @app.before_request
    def _session_store_guard() -> ResponseReturnValue | None:
        # If sessions can't be read, fail closed: refuse rather than guess.
        current = session._get_current_object()  # type: ignore[attr-defined]
        if isinstance(current, ServerSession) and current.store_unavailable:
            app.logger.error("session store unavailable")
            return jsonify({"error": "Service temporarily unavailable"}), 503
        return None

    # Order matters: throttle first, then establish identity, then CSRF
    # (which only applies to requests that carry a session identity).
    @app.before_request
    def _ip_rate_limit() -> ResponseReturnValue | None:
        if request.endpoint in _RATE_LIMIT_EXEMPT_ENDPOINTS:
            return None
        address = request.remote_addr or "unknown"
        if limiter.is_allowed(
            f"ip:{address}",
            limit=settings.rate_limit_ip_requests,
            window_seconds=settings.rate_limit_window_seconds,
        ):
            return None
        log_security_event(
            event_type="RATE_LIMIT_EXCEEDED",
            user_id="anonymous",
            status_code=429,
            reason="ip_rate_limit_exceeded",
            details={"client_ip": address},
        )
        return jsonify({"error": "Too many requests. Please try again later."}), 429

    app.before_request(load_principal)

    @app.before_request
    def _csrf_protect() -> ResponseReturnValue | None:
        principal = current_principal()
        if request.method in SAFE_METHODS or principal is None:
            return None
        reason = csrf_failure_reason()
        if reason is None:
            return None
        log_security_event(
            event_type="CSRF_REJECTED",
            user_id=principal.user_id,
            status_code=403,
            reason=reason,
            details={"path": request.path},
        )
        return jsonify({"error": "CSRF validation failed"}), 403

    @app.errorhandler(NotConfiguredError)
    def _not_configured(exc: NotConfiguredError) -> ResponseReturnValue:
        app.logger.error("%s", exc)
        return jsonify({"error": "Service temporarily unavailable"}), 503

    @app.errorhandler(RedisError)
    def _redis_unavailable(_: RedisError) -> ResponseReturnValue:
        app.logger.error("Redis unavailable")
        g.pop("principal", None)
        return jsonify({"error": "Service temporarily unavailable"}), 503

    from leyline.web import admin, auth, chat, health, monitoring

    app.register_blueprint(health.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(monitoring.bp)

    return app


if __name__ == "__main__":
    # Local development only. In containers the app runs under gunicorn.
    create_app().run()
