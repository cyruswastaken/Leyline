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
from leyline.extensions import RATE_LIMITER_KEY, REDIS_KEY, SETTINGS_KEY
from leyline.ratelimit import RedisRateLimiter
from leyline.security_headers import init_security_headers
from leyline.services.logger import log_security_event

# Probes must keep working under load and without a session store.
_RATE_LIMIT_EXEMPT_ENDPOINTS = frozenset({"health.liveness", "health.readiness"})
_STATELESS_PATHS = frozenset({"/healthz", "/readyz"})


def create_app(
    settings: Settings | None = None, *, redis_client: Redis | None = None
) -> Flask:
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

    @app.errorhandler(RedisError)
    def _redis_unavailable(_: RedisError) -> ResponseReturnValue:
        app.logger.error("Redis unavailable")
        g.pop("principal", None)
        return jsonify({"error": "Service temporarily unavailable"}), 503

    from leyline.web import auth, chat, health, monitoring

    app.register_blueprint(health.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(monitoring.bp)

    return app


if __name__ == "__main__":
    # Local development only. In containers the app runs under gunicorn.
    create_app().run()
