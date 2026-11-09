"""Request-time authentication and authorization.

Identity is loaded once per request from the server-side session and stored
on ``flask.g``. Routes opt in to protection with ``login_required`` or
``require_roles``; anything not decorated is public, which is why the access
matrix test enumerates every route in the app.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from flask import g, jsonify, redirect, request, session, url_for
from flask.typing import ResponseReturnValue

from leyline.auth.principal import AppRole, Principal
from leyline.services.logger import log_security_event

P = ParamSpec("P")
R = TypeVar("R")

PRINCIPAL_SESSION_KEY = "principal"


def load_principal() -> None:
    """before_request: restore the principal from the session, if any."""
    g.principal = Principal.from_session(session.get(PRINCIPAL_SESSION_KEY))


def current_principal() -> Principal | None:
    principal = g.get("principal")
    return principal if isinstance(principal, Principal) else None


def require_principal() -> Principal:
    """The caller's principal; only valid inside a ``login_required`` view."""
    principal = current_principal()
    if principal is None:
        raise RuntimeError("view requires login_required")
    return principal


def is_safe_redirect_target(target: str | None) -> bool:
    """Only same-site relative paths; blocks open redirects."""
    if not target or not target.startswith("/"):
        return False
    return not (target.startswith("//") or "\\" in target or "\n" in target)


def _wants_html() -> bool:
    if request.method != "GET":
        return False
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "text/html"


def _unauthenticated() -> ResponseReturnValue:
    if _wants_html():
        return redirect(
            url_for("auth.login", next=request.full_path.rstrip("?")), code=302
        )
    return jsonify({"error": "Authentication required"}), 401


def login_required(view: Callable[P, R]) -> Callable[P, Any]:
    @wraps(view)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> Any:
        if current_principal() is None:
            return _unauthenticated()
        return view(*args, **kwargs)

    return wrapped


def require_roles(*roles: AppRole) -> Callable[[Callable[P, R]], Callable[P, Any]]:
    """Allow the request only if the caller holds at least one of ``roles``."""
    allowed = frozenset(roles)

    def decorator(view: Callable[P, R]) -> Callable[P, Any]:
        @wraps(view)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> Any:
            principal = current_principal()
            if principal is None:
                return _unauthenticated()
            if not principal.has_any_role(allowed):
                log_security_event(
                    event_type="ACCESS_DENIED",
                    user_id=principal.user_id,
                    status_code=403,
                    reason="missing_role",
                    details={
                        "path": request.path,
                        "required_any": sorted(role.value for role in allowed),
                    },
                )
                return jsonify({"error": "Forbidden"}), 403
            return view(*args, **kwargs)

        return wrapped

    return decorator
