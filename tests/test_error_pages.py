"""Error pages and the landing page: styled for browsers, JSON for API callers."""

import pytest

from leyline.app import create_app
from tests.support import CSRF_HEADER, PERSONAS, login_as, make_settings

HTML = {"Accept": "text/html"}
JSON = {"Accept": "application/json"}


@pytest.fixture
def app(redis):
    flask_app = create_app(make_settings(), redis_client=redis)

    @flask_app.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail: db password hunter2")

    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def test_a_missing_page_is_a_styled_page_for_browsers(client):
    response = client.get("/no-such-page", headers=HTML)
    body = response.get_data(as_text=True)
    assert response.status_code == 404
    assert "404/" in body and "Page not found" in body
    assert 'href="/welcome"' in body  # signed out: back to the start


def test_signed_in_users_get_the_app_header_and_a_way_back_to_chat(client):
    login_as(client, PERSONAS["employee"])
    body = client.get("/no-such-page", headers=HTML).get_data(as_text=True)
    assert 'id="signout-button"' in body and 'href="/">{Back to chat}' in body


@pytest.mark.parametrize("headers", [JSON, {}, {"Accept": "*/*"}])
def test_api_callers_get_json_not_html(client, headers):
    response = client.get("/api/no-such-endpoint", headers=headers)
    assert response.status_code == 404
    assert response.get_json() == {"error": "Page not found"}


def test_non_get_requests_always_get_json(client):
    login_as(client, PERSONAS["employee"])
    response = client.post("/welcome", headers={**HTML, CSRF_HEADER: "csrf-u-employee"})
    assert response.status_code == 405
    assert response.get_json() == {"error": "Not allowed"}


def test_forbidden_is_a_styled_page_for_browsers_and_json_for_apis(client):
    login_as(client, PERSONAS["employee"])
    page = client.get("/monitoring", headers=HTML)
    assert page.status_code == 403
    assert "403/" in page.get_data(as_text=True) and "Not allowed" in page.get_data(
        as_text=True
    )
    api = client.get("/monitoring/events", headers=JSON)
    assert api.status_code == 403 and api.get_json() == {"error": "Forbidden"}


def test_an_oversized_body_is_a_json_413(client):
    login_as(client, PERSONAS["employee"])
    response = client.post(
        "/chat",
        data="x" * 100_000,
        content_type="application/json",
        headers={CSRF_HEADER: "csrf-u-employee"},
    )
    assert response.status_code == 413
    assert response.get_json()["error"] == "Too large"


@pytest.mark.parametrize("headers", [HTML, JSON])
def test_an_unexpected_error_never_leaks_its_details(app, headers):
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    response = app.test_client().get("/boom", headers=headers)
    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert "Something went wrong" in body
    assert (
        "hunter2" not in body and "RuntimeError" not in body and "Traceback" not in body
    )


def test_the_landing_page_is_public_and_static(client):
    response = client.get("/welcome", headers=HTML)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Leyline/" in body and "{Sign in}" in body
    assert 'id="hero-globe"' in body
    assert 'id="hero-dither"' in body  # the dithered landscape canvas
    # a public page must not start a session
    assert "Set-Cookie" not in response.headers


def test_signed_in_users_skip_the_landing_page(client):
    login_as(client, PERSONAS["employee"])
    response = client.get("/welcome", headers=HTML)
    assert response.status_code == 302 and response.headers["Location"] == "/"
