"""OpenID Connect client (authorization code flow with PKCE).

Works unchanged against Keycloak (local development) and Microsoft Entra ID
(Azure): both publish a standard discovery document.
"""

from __future__ import annotations

from authlib.integrations.flask_client import OAuth
from flask import Flask

from leyline.config import Settings

CLIENT_NAME = "leyline"
EXTENSION_KEY = "leyline.oauth"

# Bound every call to the identity provider (discovery, JWKS, token).
_IDP_TIMEOUT_SECONDS = 10


def init_oidc(app: Flask, settings: Settings) -> OAuth:
    oauth = OAuth(app)

    if settings.oidc_configured and settings.oidc_client_secret is not None:
        oauth.register(
            name=CLIENT_NAME,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret.get_secret_value(),
            server_metadata_url=settings.oidc_metadata_url,
            client_kwargs={
                "scope": settings.oidc_scopes,
                # PKCE: bind the authorization code to this browser session.
                "code_challenge_method": "S256",
                "default_timeout": _IDP_TIMEOUT_SECONDS,
            },
        )

    app.extensions[EXTENSION_KEY] = oauth
    return oauth
