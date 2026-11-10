"""Login, callback and logout, with the identity provider replaced by a fake.

What the fake cannot prove (Authlib's own signature/nonce/PKCE verification)
is exercised end to end against a real Keycloak; see docs/USAGE.md.
"""

import time
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import redirect, session

from leyline.app import create_app
from tests.support import PERSONAS, login_as, make_settings

JSON = {"Accept": "application/json"}
KEY_PREFIX = "leyline:session:"


class FakeIdpClient:
    def __init__(self, token=None, error=None):
        self.token = token
        self.error = error

    def authorize_redirect(self, redirect_uri):
        session["_state_leyline_fake"] = {"data": {"nonce": "n"}}  # as Authlib does
        return redirect("https://idp.example/authorize?state=fake")

    def authorize_access_token(self):
        if self.error is not None:
            raise self.error
        return self.token

    def load_server_metadata(self):
        return {"end_session_endpoint": "https://idp.example/logout"}


class FakeOAuth:
    def __init__(self, client):
        self.client = client

    def create_client(self, name):
        return self.client


def with_idp(app, **kwargs):
    app.extensions["leyline.oauth"] = FakeOAuth(FakeIdpClient(**kwargs))


def good_token(**claims):
    base = {
        "sub": "kc-user-1",
        "name": "Eve",
        "email": "eve@example.test",
        "groups": ["staff"],
        "roles": [],
    }
    base.update(claims)
    return {"userinfo": base, "id_token": "header.payload.signature"}


def sid(client):
    cookie = client.get_cookie("leyline_sid")
    return cookie.value if cookie else None


# --- login redirect ---------------------------------------------------------


def test_login_sends_the_browser_to_the_idp_with_state_nonce_and_pkce(app, redis):
    oauth_client = app.extensions["leyline.oauth"].create_client("leyline")
    oauth_client.server_metadata.update(
        {
            "issuer": "https://idp.invalid",
            "authorization_endpoint": "https://idp.invalid/auth",
            "token_endpoint": "https://idp.invalid/token",
            "jwks_uri": "https://idp.invalid/jwks",
            "_loaded_at": time.time(),
        }
    )
    client = app.test_client()

    response = client.get("/login?next=/monitoring")

    assert response.status_code == 302
    location = urlsplit(response.headers["Location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        "https://idp.invalid/auth"
    )
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == "leyline-web"
    assert query["redirect_uri"].endswith("/auth/callback")
    assert "openid" in query["scope"].split()
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) >= 43
    assert len(query["state"]) >= 16
    assert len(query["nonce"]) >= 16
    assert "code_verifier" not in query  # the secret half never leaves the server

    # State, nonce and verifier are held server-side, bound to this browser.
    stored = redis.get(KEY_PREFIX + sid(client))
    assert query["state"] in stored
    assert "code_verifier" in stored


def test_login_is_unavailable_when_no_identity_provider_is_configured(redis, llm_calls):
    app = create_app(
        make_settings(
            oidc_metadata_url=None, oidc_client_id=None, oidc_client_secret=None
        ),
        redis_client=redis,
    )
    assert app.test_client().get("/login").status_code == 503


def test_signed_in_users_skip_the_idp(app):
    client = app.test_client()
    login_as(client, PERSONAS["employee"])
    response = client.get("/login")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


# --- callback ---------------------------------------------------------------


def test_successful_callback_creates_an_authenticated_session(app, events):
    with_idp(app, token=good_token(roles=["security_auditor"], groups=["staff", "hr"]))
    client = app.test_client()

    response = client.get("/auth/callback?code=abc&state=fake")

    assert response.status_code == 302
    me = client.get("/me", headers=JSON).get_json()
    assert me["user_id"] == "kc-user-1"
    assert me["roles"] == ["security_auditor", "user"]
    assert me["groups"] == ["hr", "staff"]
    assert me["csrf_token"]
    assert [e["event_type"] for e in events] == ["AUTH_LOGIN"]
    assert events[0]["user_id"] == "kc-user-1"


def test_login_rotates_the_session_id_and_destroys_the_old_one(app, redis):
    with_idp(app, token=good_token())
    client = app.test_client()
    client.get("/login")  # pre-login session (holds OIDC state)
    before = sid(client)
    assert before and redis.get(KEY_PREFIX + before)

    client.get("/auth/callback?code=abc&state=fake")

    after = sid(client)
    assert after != before
    assert redis.get(KEY_PREFIX + before) is None
    assert redis.get(KEY_PREFIX + after) is not None


def test_a_planted_session_id_is_not_promoted_to_an_authenticated_one(app):
    with_idp(app, token=good_token())
    victim = app.test_client()
    victim.get("/login")
    planted = sid(victim)  # attacker made the victim use this session id

    victim.get("/auth/callback?code=abc&state=fake")

    attacker = app.test_client()
    attacker.set_cookie("leyline_sid", planted)
    assert attacker.get("/me", headers=JSON).status_code == 401


def test_callback_returns_to_the_page_the_user_wanted(app):
    with_idp(app, token=good_token())
    client = app.test_client()
    client.get("/login?next=/monitoring")
    response = client.get("/auth/callback?code=abc&state=fake")
    assert response.headers["Location"] == "/monitoring"


@pytest.mark.parametrize(
    "target",
    [
        "//evil.example",
        "https://evil.example/x",
        "/\\evil.example",
        "evil",
        "javascript:alert(1)",
    ],
)
def test_open_redirects_are_ignored(app, target):
    with_idp(app, token=good_token())
    client = app.test_client()
    client.get("/login", query_string={"next": target})
    response = client.get("/auth/callback?code=abc&state=fake")
    assert response.headers["Location"] == "/"


@pytest.mark.parametrize(
    "idp",
    [
        {"error": RuntimeError("state mismatch: secret-detail-123")},
        {"error": ValueError("bad signature")},
        {"token": {}},  # no verified identity at all
        {"token": {"userinfo": "not-a-mapping"}},
        {"token": {"userinfo": {"name": "no subject"}}},
    ],
)
def test_any_sign_in_failure_fails_closed_without_leaking_details(app, events, idp):
    with_idp(app, **idp)
    client = app.test_client()

    response = client.get("/auth/callback?code=abc&state=fake")

    assert response.status_code == 400
    assert "secret-detail-123" not in response.get_data(as_text=True)
    assert client.get("/me", headers=JSON).status_code == 401
    assert [e["event_type"] for e in events] == ["AUTH_FAILED"]


def test_a_failed_sign_in_shows_the_styled_page_with_a_way_to_retry(app):
    with_idp(app, error=ValueError("bad signature"))
    client = app.test_client()

    response = client.get("/auth/callback?code=abc&state=fake")
    body = response.get_data(as_text=True)

    assert response.status_code == 400
    assert "Sign-in failed" in body and 'href="/login">{Try again}</a>' in body
    # the same layout as every other page: a stylesheet from /static/dist, no inline code
    assert "<style" not in body and "<script>" not in body
    assert "bad signature" not in body


def test_forged_identity_headers_and_params_are_ignored(app):
    with_idp(app, error=RuntimeError("no valid code"))
    client = app.test_client()
    response = client.get(
        "/auth/callback?code=x&state=y&user_id=admin&role=security_auditor",
        headers={
            "X-User": "admin",
            "X-Forwarded-User": "admin",
            "Remote-User": "admin",
        },
    )
    assert response.status_code == 400
    assert client.get("/monitoring/events", headers=JSON).status_code == 401


# --- logout -----------------------------------------------------------------


def test_logout_requires_a_csrf_token(app, events):
    client = app.test_client()
    login_as(client, PERSONAS["employee"])

    response = client.post("/logout")

    assert response.status_code == 403
    assert client.get("/me", headers=JSON).status_code == 200  # still signed in
    assert events[-1]["event_type"] == "CSRF_REJECTED"


def test_logout_ends_the_idp_session_too(app, events):
    with_idp(app, token=good_token())
    client = app.test_client()
    client.get("/auth/callback?code=abc&state=fake")
    token = client.get("/me", headers=JSON).get_json()["csrf_token"]

    response = client.post("/logout", headers={"X-CSRF-Token": token})

    url = urlsplit(response.get_json()["redirect"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://idp.example/logout"
    query = parse_qs(url.query)
    assert query["id_token_hint"] == ["header.payload.signature"]
    assert query["client_id"] == ["leyline-web"]
    assert query["post_logout_redirect_uri"][0].endswith("/")
    assert client.get("/me", headers=JSON).status_code == 401
    assert events[-1]["event_type"] == "AUTH_LOGOUT"


def test_logout_without_session_is_unauthorized(client):
    assert client.post("/logout").status_code == 401


# --- identity provider outage -----------------------------------------------


def test_unreachable_identity_provider_gives_a_clean_503(client, events):
    # The configured metadata host does not resolve; discovery fails.
    response = client.get("/login")
    assert response.status_code == 503
    assert response.get_json() == {"error": "Identity provider unavailable"}
    assert "Content-Security-Policy" in response.headers
    assert events[-1]["event_type"] == "AUTH_FAILED"
    assert events[-1]["reason"].startswith("idp_unavailable")
