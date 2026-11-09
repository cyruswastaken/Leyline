"""CSRF protection for cookie-authenticated, state-changing requests.

Defence in depth: SameSite=Lax cookies, an Origin check, and a per-session
token that must be echoed in a header (or form field) that a cross-site page
cannot set.
"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from flask import request, session

CSRF_SESSION_KEY = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def new_csrf_token() -> str:
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def get_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if isinstance(token, str) and token:
        return token
    return new_csrf_token()


def csrf_failure_reason() -> str | None:
    """Return why this unsafe request must be rejected, or None if it's fine."""
    origin = request.headers.get("Origin")
    if origin is not None and not _same_origin(origin):
        return "origin_mismatch"

    expected = session.get(CSRF_SESSION_KEY)
    supplied = request.headers.get(CSRF_HEADER) or request.form.get(CSRF_FORM_FIELD)

    if not isinstance(expected, str) or not expected:
        return "no_session_token"
    if not supplied:
        return "missing_token"
    if not hmac.compare_digest(expected.encode(), supplied.encode()):
        return "token_mismatch"
    return None


def _same_origin(origin: str) -> bool:
    parts = urlsplit(origin)
    target = urlsplit(request.host_url)
    return (parts.scheme, parts.netloc) == (target.scheme, target.netloc)
