import pytest

from leyline.adapters.blob_memory import InMemoryBlobStore
from leyline.app import create_app
from leyline.ratelimit import RedisRateLimiter
from tests.support import (
    PERSONAS,
    FakeSearchIndex,
    StubDatabase,
    login_as,
    make_settings,
)

JSON = {"Accept": "application/json"}


def _build(settings, redis):
    return create_app(
        settings,
        redis_client=redis,
        database=StubDatabase(),
        blob_store=InMemoryBlobStore(),
        search_index=FakeSearchIndex(),
    )


# --- the limiter itself -----------------------------------------------------


def test_allows_up_to_the_limit_then_denies(redis):
    limiter = RedisRateLimiter(redis)
    results = [limiter.is_allowed("k", limit=3, window_seconds=60) for _ in range(5)]
    assert results == [True, True, True, False, False]


def test_keys_are_independent(redis):
    limiter = RedisRateLimiter(redis)
    for _ in range(3):
        limiter.is_allowed("a", limit=3, window_seconds=60)
    assert limiter.is_allowed("a", limit=3, window_seconds=60) is False
    assert limiter.is_allowed("b", limit=3, window_seconds=60) is True


def test_window_slides(redis, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("leyline.ratelimit.time.time", lambda: now[0])
    limiter = RedisRateLimiter(redis)

    assert limiter.is_allowed("k", limit=2, window_seconds=10)
    now[0] += 4
    assert limiter.is_allowed("k", limit=2, window_seconds=10)
    assert not limiter.is_allowed("k", limit=2, window_seconds=10)
    now[0] += 7  # first hit (t=1000) has aged out, second (t=1004) has not
    assert limiter.is_allowed("k", limit=2, window_seconds=10)
    assert not limiter.is_allowed("k", limit=2, window_seconds=10)


def test_rejected_attempts_do_not_extend_the_lockout(redis, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("leyline.ratelimit.time.time", lambda: now[0])
    limiter = RedisRateLimiter(redis)

    assert limiter.is_allowed("k", limit=1, window_seconds=10)
    for _ in range(20):  # a client hammering while blocked
        now[0] += 0.4
        assert not limiter.is_allowed("k", limit=1, window_seconds=10)
    now[0] = 1010.5  # 10.5s after the one accepted hit
    assert limiter.is_allowed("k", limit=1, window_seconds=10)


def test_limit_is_shared_between_app_instances(redis, llm_calls):
    """Two workers/instances behind a load balancer share one budget."""
    settings = make_settings(rate_limit_requests=4)
    first = _build(settings, redis).test_client()
    second = _build(settings, redis).test_client()
    t1 = login_as(first, PERSONAS["employee"])
    t2 = login_as(second, PERSONAS["employee"])

    statuses = []
    for client, token in [(first, t1), (second, t2)] * 3:
        response = client.post(
            "/chat",
            json={"message": "vpn"},
            headers={"X-CSRF-Token": token},
        )
        statuses.append(response.status_code)

    assert statuses == [200, 200, 200, 200, 429, 429]


# --- applied to requests ----------------------------------------------------


def chat(client, token):
    return client.post(
        "/chat", json={"message": "vpn"}, headers={"X-CSRF-Token": token}
    )


def test_per_user_limit_returns_429_with_a_clear_body(app, login, client, events):
    token = login("employee")
    limit = app.extensions["leyline.settings"].rate_limit_requests

    for _ in range(limit):
        assert chat(client, token).status_code == 200
    response = chat(client, token)

    assert response.status_code == 429
    assert response.get_json()["reason"] == "rate_limit_exceeded"
    assert any(
        e["event_type"] == "RATE_LIMIT_EXCEEDED" and e["user_id"] == "u-employee"
        for e in events
    )


def test_one_users_limit_does_not_affect_another(app):
    a, b = app.test_client(), app.test_client()
    ta = login_as(a, PERSONAS["employee"])
    tb = login_as(b, PERSONAS["security"])
    limit = app.extensions["leyline.settings"].rate_limit_requests

    for _ in range(limit + 1):
        chat(a, ta)

    assert chat(a, ta).status_code == 429
    assert chat(b, tb).status_code == 200


def test_ip_limit_throttles_anonymous_traffic(redis, llm_calls, events):
    app = create_app(make_settings(rate_limit_ip_requests=5), redis_client=redis)
    client = app.test_client()

    codes = [client.get("/login").status_code for _ in range(8)]

    assert codes[:5] != [429] * 5 and 429 not in codes[:5]
    assert codes[5:] == [429, 429, 429]
    assert any(e["reason"] == "ip_rate_limit_exceeded" for e in events)


def test_health_probes_are_never_rate_limited(redis, llm_calls):
    app = create_app(make_settings(rate_limit_ip_requests=2), redis_client=redis)
    client = app.test_client()
    assert all(client.get("/healthz").status_code == 200 for _ in range(20))


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Real-IP"])
def test_spoofed_forwarding_headers_do_not_evade_the_ip_limit(redis, llm_calls, header):
    # No trusted proxy is configured, so client-supplied headers are ignored.
    app = create_app(make_settings(rate_limit_ip_requests=3), redis_client=redis)
    client = app.test_client()
    codes = [
        client.get("/login", headers={header: f"10.0.0.{i}"}).status_code
        for i in range(6)
    ]
    assert codes[3:] == [429, 429, 429]


def test_trusted_proxy_address_is_used_when_configured(redis, llm_calls):
    app = create_app(
        make_settings(rate_limit_ip_requests=2, trusted_proxy_count=1),
        redis_client=redis,
    )
    client = app.test_client()

    def hit(ip):
        return client.get("/login", headers={"X-Forwarded-For": ip}).status_code

    assert hit("203.0.113.1") != 429
    assert hit("203.0.113.1") != 429
    assert hit("203.0.113.1") == 429
    assert hit("203.0.113.2") != 429  # a different real client is unaffected
