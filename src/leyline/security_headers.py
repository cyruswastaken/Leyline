"""Browser-facing security headers, including a per-request CSP nonce."""

from __future__ import annotations

import secrets
from typing import Any

from flask import Flask, Response, g

from leyline.auth.csrf import get_csrf_token
from leyline.config import Settings


def init_security_headers(app: Flask, settings: Settings) -> None:
    @app.before_request
    def _make_nonce() -> None:
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.context_processor
    def _template_context() -> dict[str, Any]:
        return {
            "csp_nonce": g.get("csp_nonce", ""),
            # A callable so the token (and thus a session) is only created
            # by pages that actually need it.
            "csrf_token": get_csrf_token,
        }

    @app.after_request
    def _apply(response: Response) -> Response:
        nonce = g.get("csp_nonce", "")
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'nonce-{nonce}'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        # Responses contain per-user data and must never be shared-cached.
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.session_cookie_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response
