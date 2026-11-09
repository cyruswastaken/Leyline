import json
import time

import pytest

from leyline.app import create_app
from tests.support import PERSONAS, login_as, make_settings

KEY_PREFIX = "leyline:session:"


def session_cookie(client):
    cookie = client.get_cookie("leyline_sid")
    return cookie.value if cookie else None


def test_cookie_is_an_opaque_id_and_data_stays_on_the_server(client, redis):
    login_as(client, PERSONAS["security"])
    sid = session_cookie(client)

    assert sid and len(sid) == 43
    assert "security" not in sid and "u-security" not in sid
    stored = json.loads(redis.get(KEY_PREFIX + sid))
    assert stored["data"]["principal"]["user_id"] == "u-security"


def test_cookie_attributes(client):
    login_as(client, PERSONAS["employee"])
    cookie = client.get_cookie("leyline_sid")
    assert cookie.http_only is True
    assert cookie.same_site == "Lax"
    assert cookie.path == "/"
    assert cookie.secure is False  # plain-HTTP local development only


def test_secure_deployments_use_host_prefixed_secure_cookies(redis, llm_calls):
    app = create_app(make_settings(session_cookie_secure=True), redis_client=redis)
    client = app.test_client()
    login_as(client, PERSONAS["employee"])
    cookie = client.get_cookie("__Host-leyline_sid")
    assert cookie is not None
    assert cookie.secure is True
    assert cookie.http_only is True
    assert cookie.path == "/"
    assert cookie.domain in (None, "", "localhost")


def test_session_has_an_idle_timeout(client, redis, settings):
    login_as(client, PERSONAS["employee"])
    ttl = redis.ttl(KEY_PREFIX + session_cookie(client))
    assert 0 < ttl <= settings.session_idle_seconds


def test_activity_extends_the_idle_timeout(client, redis):
    login_as(client, PERSONAS["employee"])
    key = KEY_PREFIX + session_cookie(client)
    redis.expire(key, 5)

    client.get("/me", headers={"Accept": "application/json"})

    assert redis.ttl(key) > 5


def test_session_expires_at_the_absolute_lifetime_even_if_active(
    client, redis, settings
):
    login_as(client, PERSONAS["employee"])
    key = KEY_PREFIX + session_cookie(client)
    stored = json.loads(redis.get(key))
    stored["created_at"] = time.time() - settings.session_absolute_seconds - 1
    redis.set(key, json.dumps(stored), ex=600)

    response = client.get("/me", headers={"Accept": "application/json"})

    assert response.status_code == 401
    assert redis.get(key) is None


def test_expired_server_session_means_signed_out(client, redis):
    login_as(client, PERSONAS["employee"])
    redis.flushall()  # what an idle expiry looks like to the app
    response = client.get("/me", headers={"Accept": "application/json"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "cookie_value",
    ["not-a-valid-id", "A" * 43, "../../etc/passwd", "x" * 500, ""],
)
def test_forged_or_malformed_session_ids_are_anonymous(client, cookie_value):
    client.set_cookie("leyline_sid", cookie_value)
    response = client.get("/me", headers={"Accept": "application/json"})
    assert response.status_code == 401


def test_a_session_whose_stored_data_is_corrupt_is_discarded(client, redis):
    login_as(client, PERSONAS["employee"])
    key = KEY_PREFIX + session_cookie(client)
    redis.set(key, "{not json", ex=600)

    assert client.get("/me", headers={"Accept": "application/json"}).status_code == 401
    assert redis.get(key) is None


def test_anonymous_requests_do_not_create_sessions(client, redis):
    client.get("/healthz")
    client.get("/me", headers={"Accept": "application/json"})
    assert redis.keys(KEY_PREFIX + "*") == []
    assert client.get_cookie("leyline_sid") is None


def test_logout_destroys_the_server_side_session(client, redis):
    token = login_as(client, PERSONAS["employee"])
    key = KEY_PREFIX + session_cookie(client)

    response = client.post("/logout", headers={"X-CSRF-Token": token})

    assert response.status_code == 200
    assert redis.get(key) is None
    assert client.get("/me", headers={"Accept": "application/json"}).status_code == 401


def test_a_stolen_session_id_stops_working_after_logout(app, redis):
    victim = app.test_client()
    token = login_as(victim, PERSONAS["employee"])
    stolen = session_cookie(victim)

    attacker = app.test_client()
    attacker.set_cookie("leyline_sid", stolen)
    assert (
        attacker.get("/me", headers={"Accept": "application/json"}).status_code == 200
    )

    victim.post("/logout", headers={"X-CSRF-Token": token})

    assert (
        attacker.get("/me", headers={"Accept": "application/json"}).status_code == 401
    )
