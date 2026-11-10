from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

from leyline.auth.access import (
    PRINCIPAL_SESSION_KEY,
    current_principal,
    is_safe_redirect_target,
    login_required,
    require_principal,
)
from leyline.auth.csrf import get_csrf_token, new_csrf_token
from leyline.auth.oidc import CLIENT_NAME, EXTENSION_KEY
from leyline.auth.principal import InvalidClaimsError, principal_from_claims
from leyline.auth.sessions import rotate_session_id
from leyline.extensions import app_settings
from leyline.services.logger import log_security_event

bp = Blueprint("auth", __name__)

_REDIRECT_KEY = "post_login_redirect"
_ID_TOKEN_KEY = "id_token"  # noqa: S105 (session key name, not a secret)


def _client() -> Any:
    oauth = current_app.extensions.get(EXTENSION_KEY)
    return oauth.create_client(CLIENT_NAME) if oauth is not None else None


def _sign_in_failed() -> Response:
    # Always a browser navigation (the identity provider redirects here), so the
    # styled error page, like every other page, with a way to start again.
    return Response(
        render_template(
            "error.html",
            code=400,
            title="Sign-in failed",
            message="The sign-in could not be completed. Please try again.",
            user=None,
            action_href="/login",
            action_label="Try again",
        ),
        status=400,
        mimetype="text/html",
    )


@bp.get("/login")
def login() -> ResponseReturnValue:
    target = request.args.get("next")
    safe_target = target if is_safe_redirect_target(target) else None

    if current_principal() is not None:
        return redirect(safe_target or url_for("chat.index"))

    client = _client()
    if client is None:
        return jsonify({"error": "Sign-in is not configured"}), 503

    if safe_target:
        session[_REDIRECT_KEY] = safe_target

    redirect_uri = url_for("auth.callback", _external=True)
    # Authlib adds state, nonce and the PKCE challenge, and keeps the
    # matching secrets in the server-side session.
    try:
        response: ResponseReturnValue = client.authorize_redirect(redirect_uri)
    except Exception as exc:  # discovery document unreachable, bad JSON, ...
        current_app.logger.error(
            "identity provider unavailable: %s", type(exc).__name__
        )
        log_security_event(
            event_type="AUTH_FAILED",
            user_id="anonymous",
            status_code=503,
            reason=f"idp_unavailable:{type(exc).__name__}",
        )
        return jsonify({"error": "Identity provider unavailable"}), 503
    return response


@bp.get("/auth/callback")
def callback() -> ResponseReturnValue:
    client = _client()
    if client is None:
        return jsonify({"error": "Sign-in is not configured"}), 503

    settings = app_settings()

    try:
        # Verifies state, exchanges the code (with the PKCE verifier) and
        # validates the ID token's signature, issuer, audience, expiry and
        # nonce.
        token = client.authorize_access_token()
        claims = token.get("userinfo")
        if not isinstance(claims, Mapping):
            raise InvalidClaimsError("no verified identity in token")
        principal = principal_from_claims(
            claims,
            groups_claim=settings.oidc_groups_claim,
            roles_claim=settings.oidc_roles_claim,
        )
    except Exception as exc:  # fail closed on *any* sign-in problem
        current_app.logger.warning("sign-in failed: %s", type(exc).__name__)
        log_security_event(
            event_type="AUTH_FAILED",
            user_id="anonymous",
            status_code=400,
            reason=type(exc).__name__,
        )
        session.clear()
        return _sign_in_failed()

    target = session.get(_REDIRECT_KEY)
    id_token = token.get("id_token")

    # Start from a clean session with a brand-new id so a session id planted
    # before login (session fixation) is never promoted to an authenticated one.
    session.clear()
    session[PRINCIPAL_SESSION_KEY] = principal.to_session()
    if isinstance(id_token, str):
        session[_ID_TOKEN_KEY] = id_token
    new_csrf_token()
    rotate_session_id()

    log_security_event(
        event_type="AUTH_LOGIN",
        user_id=principal.user_id,
        status_code=200,
        reason="login_succeeded",
        details={"groups_incomplete": True} if principal.groups_incomplete else None,
    )

    destination = (
        target
        if isinstance(target, str) and is_safe_redirect_target(target)
        else url_for("chat.index")
    )
    return redirect(destination)


@bp.post("/logout")
@login_required
def logout() -> ResponseReturnValue:
    principal = current_principal()
    id_token = session.get(_ID_TOKEN_KEY)
    end_session_url = _end_session_url(id_token if isinstance(id_token, str) else None)

    session.clear()

    if principal is not None:
        log_security_event(
            event_type="AUTH_LOGOUT",
            user_id=principal.user_id,
            status_code=200,
            reason="logout",
        )

    # The browser then visits the identity provider to end its single
    # sign-on session too; otherwise the next "login" would be silent.
    return jsonify({"redirect": end_session_url or url_for("chat.index")})


@bp.get("/me")
@login_required
def me() -> ResponseReturnValue:
    principal = require_principal()
    return jsonify(
        {
            "user_id": principal.user_id,
            "name": principal.name,
            "email": principal.email,
            "roles": sorted(role.value for role in principal.roles),
            "groups": sorted(principal.groups),
            "csrf_token": get_csrf_token(),
        }
    )


def _end_session_url(id_token: str | None) -> str | None:
    client = _client()
    if client is None:
        return None
    try:
        endpoint = client.load_server_metadata().get("end_session_endpoint")
    except Exception:
        current_app.logger.warning("could not load identity provider metadata")
        return None
    if not isinstance(endpoint, str):
        return None

    params = {
        "client_id": app_settings().oidc_client_id or "",
        "post_logout_redirect_uri": url_for("chat.index", _external=True),
    }
    if id_token:
        params["id_token_hint"] = id_token
    return f"{endpoint}?{urlencode(params)}"
