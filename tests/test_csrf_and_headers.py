import re

import pytest

from leyline.app import create_app
from tests.support import PERSONAS, login_as, make_settings

JSON = {"Accept": "application/json"}


def post_chat(client, headers=None):
    return client.post(
        "/chat", json={"message": "How do I set up the VPN?"}, headers=headers or {}
    )


# --- CSRF -------------------------------------------------------------------


def test_signed_in_post_without_a_token_is_rejected(client, events, llm_calls):
    login_as(client, PERSONAS["employee"])
    response = post_chat(client)
    assert response.status_code == 403
    assert llm_calls == []
    rejected = [e for e in events if e["event_type"] == "CSRF_REJECTED"]
    assert rejected and rejected[0]["reason"] == "missing_token"


def test_signed_in_post_with_the_wrong_token_is_rejected(client, events):
    login_as(client, PERSONAS["employee"])
    response = post_chat(client, {"X-CSRF-Token": "guess"})
    assert response.status_code == 403
    assert events[-1]["reason"] == "token_mismatch"


def test_a_token_from_another_session_is_rejected(app):
    alice, bob = app.test_client(), app.test_client()
    login_as(alice, PERSONAS["employee"])
    bob_token = login_as(bob, PERSONAS["security"])
    assert post_chat(alice, {"X-CSRF-Token": bob_token}).status_code == 403


def test_the_correct_token_is_accepted(client, llm_calls):
    token = login_as(client, PERSONAS["employee"])
    assert post_chat(client, {"X-CSRF-Token": token}).status_code == 200


def test_a_cross_site_origin_is_rejected_even_with_a_valid_token(client, events):
    token = login_as(client, PERSONAS["employee"])
    response = post_chat(
        client, {"X-CSRF-Token": token, "Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert events[-1]["reason"] == "origin_mismatch"


def test_the_same_origin_is_accepted(client):
    token = login_as(client, PERSONAS["employee"])
    response = post_chat(client, {"X-CSRF-Token": token, "Origin": "http://localhost"})
    assert response.status_code == 200


def test_the_token_can_be_sent_as_a_form_field_too(client):
    token = login_as(client, PERSONAS["employee"])
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 200


def test_safe_methods_do_not_need_a_token(client):
    login_as(client, PERSONAS["employee"])
    assert client.get("/me", headers=JSON).status_code == 200


def test_anonymous_post_is_unauthorized_not_a_csrf_error(client):
    assert post_chat(client).status_code == 401


# --- security headers -------------------------------------------------------


def test_csp_uses_a_fresh_nonce_and_no_unsafe_inline(client):
    login_as(client, PERSONAS["employee"])
    first = client.get("/", headers={"Accept": "text/html"})
    second = client.get("/", headers={"Accept": "text/html"})

    csp = first.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "font-src 'self'" in csp

    nonce = re.search(r"script-src 'self' 'nonce-([^']+)'", csp).group(1)
    other = re.search(
        r"script-src 'self' 'nonce-([^']+)'", second.headers["Content-Security-Policy"]
    ).group(1)
    assert nonce != other


PAGES = [
    ("employee", "/"),
    ("employee", "/tickets"),
    ("kb_admin", "/admin/documents"),
    ("auditor", "/monitoring"),
    (None, "/welcome"),
    (None, "/no-such-page"),
]


@pytest.mark.parametrize(("persona", "path"), PAGES)
def test_pages_have_no_inline_script_or_style_at_all(client, persona, path):
    """All behaviour and styling is in compiled files from /static/dist, so the
    policy needs no inline exceptions and a page holds none."""
    if persona:
        login_as(client, PERSONAS[persona])
    html = client.get(path, headers={"Accept": "text/html"}).get_data(as_text=True)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), path
    assert not re.search(r"<style[^>]*>", html), path
    assert " onclick=" not in html and " onload=" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"javascript:", html)


@pytest.mark.parametrize("path", ["/healthz", "/me", "/login", "/nope"])
def test_baseline_headers_on_every_response(client, path):
    headers = client.get(path).headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"
    assert "Content-Security-Policy" in headers
    assert "Strict-Transport-Security" not in headers  # plain HTTP in dev


def test_hsts_is_sent_when_cookies_are_secure(redis, llm_calls):
    app = create_app(make_settings(session_cookie_secure=True), redis_client=redis)
    headers = app.test_client().get("/healthz").headers
    assert "max-age=31536000" in headers["Strict-Transport-Security"]


def test_the_csrf_token_is_embedded_for_the_page_script(client):
    token = login_as(client, PERSONAS["employee"])
    html = client.get("/", headers={"Accept": "text/html"}).get_data(as_text=True)
    assert f'<meta name="csrf-token" content="{token}">' in html


def test_user_supplied_names_are_html_escaped(client):
    from leyline.auth.principal import Principal

    login_as(client, Principal(user_id="u", name="<script>alert(1)</script>"))
    html = client.get("/", headers={"Accept": "text/html"}).get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
