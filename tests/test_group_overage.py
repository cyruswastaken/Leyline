"""Entra group overage: resolving a user's groups through Microsoft Graph, safely."""

import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from leyline.app import create_app
from leyline.auth import graph as graph_module
from leyline.auth.graph import GraphError, fetch_group_ids
from leyline.auth.principal import (
    Principal,
    principal_from_claims,
    with_resolved_groups,
)
from leyline.config import Settings
from leyline.services.monitoring import get_recent_events
from tests.support import FakeChatModel, make_settings

HR_ID = "11111111-1111-1111-1111-111111111111"
STAFF_ID = "22222222-2222-2222-2222-222222222222"
STRANGER_ID = "99999999-9999-9999-9999-999999999999"
NAMES = {HR_ID: "hr", STAFF_ID: "staff"}

OVERAGE_CLAIMS = {
    "sub": "u-overage",
    "name": "Over Age",
    "_claim_names": {"groups": "src1"},
    "_claim_sources": {"src1": {"endpoint": "https://evil.example/steal"}},
}


# --- the pure parts -----------------------------------------------------------------------


def test_resolved_ids_become_only_the_names_that_were_mapped():
    start = principal_from_claims(OVERAGE_CLAIMS, group_names=NAMES)
    assert start.groups_incomplete and start.groups == frozenset()

    done = with_resolved_groups(start, [HR_ID, STRANGER_ID, STAFF_ID], NAMES)

    assert done.groups == {"hr", "staff"}  # the unmapped stranger grants nothing
    assert done.groups_incomplete is False
    assert done.user_id == start.user_id and done.roles == start.roles


def test_without_a_mapping_nothing_is_resolved():
    start = Principal(user_id="u", groups_incomplete=True)
    assert with_resolved_groups(start, [HR_ID], {}) is start


def test_resolution_never_keeps_groups_the_lookup_did_not_return():
    start = Principal(
        user_id="u", groups=frozenset({"finance"}), groups_incomplete=True
    )
    assert with_resolved_groups(start, [HR_ID], NAMES).groups == {"hr"}


# --- the Graph client against a local server ----------------------------------------------


class FakeGraph:
    def __init__(self, *, status=200, replies=None):
        self.requests = []
        self.status = status
        self.replies = list(replies or [{"value": [HR_ID, STAFF_ID]}])
        outer = self

        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b'{"error": "secret detail"}')
                    return
                reply = (
                    outer.replies.pop(0) if len(outer.replies) > 1 else outer.replies[0]
                )
                data = (
                    json.dumps(reply).encode()
                    if not isinstance(reply, bytes)
                    else reply
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self._server.server_port}/v1.0"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def graph():
    servers = []

    def make(**kwargs):
        server = FakeGraph(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_the_client_asks_graph_for_the_users_own_groups_with_their_token(graph):
    server = graph()
    ids = fetch_group_ids("user-access-token", base_url=server.base, timeout_seconds=2)
    assert ids == [HR_ID, STAFF_ID]
    request = server.requests[0]
    assert request["path"] == "/v1.0/me/getMemberObjects"
    assert request["auth"] == "Bearer user-access-token"
    assert request["body"] == {"securityEnabledOnly": True}


def test_paging_follows_the_next_link_only_on_the_configured_host(graph):
    server = graph()
    server.replies = [
        {
            "value": [HR_ID],
            "@odata.nextLink": server.base + "/me/getMemberObjects?page=2",
        },
        {"value": [STAFF_ID, HR_ID]},
    ]
    assert fetch_group_ids("t", base_url=server.base, timeout_seconds=2) == [
        HR_ID,
        STAFF_ID,
    ]

    elsewhere = graph(
        replies=[{"value": [HR_ID], "@odata.nextLink": "https://evil.example/next"}]
    )
    assert fetch_group_ids("t", base_url=elsewhere.base, timeout_seconds=2) == [HR_ID]
    assert len(elsewhere.requests) == 1  # the foreign link was never followed


def test_a_runaway_paging_loop_stops(graph):
    server = graph()
    server.replies = [{"value": [HR_ID], "@odata.nextLink": server.base + "/me/x"}]
    with pytest.raises(GraphError) as caught:
        fetch_group_ids("t", base_url=server.base, timeout_seconds=2)
    assert caught.value.reason == "too_many_pages"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"status": 401}, "http_401"),
        ({"status": 403}, "http_403"),
        ({"status": 503}, "http_503"),
        ({"replies": [{"value": "not a list"}]}, "bad_response"),
        ({"replies": [{"value": [1, 2]}]}, "bad_response"),
        ({"replies": [{"other": []}]}, "bad_response"),
        ({"replies": [b"not json"]}, "bad_response"),
    ],
)
def test_any_failure_is_a_graph_error_without_leaking_detail(graph, kwargs, reason):
    server = graph(**kwargs)
    with pytest.raises(GraphError) as caught:
        fetch_group_ids("t", base_url=server.base, timeout_seconds=2)
    assert caught.value.reason == reason
    assert "secret detail" not in str(caught.value)


def test_no_access_token_means_no_call(graph):
    server = graph()
    with pytest.raises(GraphError) as caught:
        fetch_group_ids("", base_url=server.base)
    assert caught.value.reason == "no_access_token" and server.requests == []


def test_an_unreachable_graph_is_a_connection_error():
    with pytest.raises(GraphError) as caught:
        fetch_group_ids("t", base_url="http://127.0.0.1:9/v1.0", timeout_seconds=1)
    assert caught.value.reason == "connection_error"


# --- configuration --------------------------------------------------------------------------


def settings_with(**overrides):
    base = {"_env_file": None, "oidc_resolve_group_overage": True}
    return Settings(**{**base, **overrides})


def test_overage_resolution_is_off_by_default():
    assert Settings(_env_file=None).oidc_resolve_group_overage is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({}, "OIDC_GROUP_NAMES"),
        ({"oidc_group_names": NAMES}, "GroupMember.Read.All"),
        (
            {
                "oidc_group_names": NAMES,
                "oidc_scopes": "openid GroupMember.Read.All",
                "graph_base_url": "http://graph.example/v1.0",
            },
            "https",
        ),
    ],
)
def test_overage_resolution_refuses_an_unsafe_configuration(overrides, message):
    with pytest.raises(ValidationError, match=message):
        settings_with(**overrides)


def test_overage_resolution_accepts_a_complete_configuration():
    s = settings_with(
        oidc_group_names=NAMES, oidc_scopes="openid profile email GroupMember.Read.All"
    )
    assert s.oidc_resolve_group_overage


# --- inside sign-in ------------------------------------------------------------------------


class FakeOidcClient:
    def __init__(self, claims, access_token="graph-access-token"):  # noqa: S107
        self._token = {
            "userinfo": claims,
            "access_token": access_token,
            "id_token": "the-id-token",
        }

    def authorize_access_token(self):
        return self._token


@pytest.fixture
def make_client(db, redis, blobs, embedder, monkeypatch):
    def _make(claims, *, resolve=True, ids=None, error=None, calls=None):
        overrides = {"oidc_group_names": NAMES}
        if resolve:
            overrides.update(
                oidc_resolve_group_overage=True,
                oidc_scopes="openid profile email GroupMember.Read.All",
            )
        app = create_app(
            make_settings(**overrides),
            redis_client=redis,
            database=db,
            blob_store=blobs,
            embedder=embedder,
            chat_model=FakeChatModel(),
        )
        app.config.update(TESTING=True)
        monkeypatch.setattr("leyline.web.auth._client", lambda: FakeOidcClient(claims))

        def fake_fetch(access_token, **kwargs):
            if calls is not None:
                calls.append((access_token, kwargs))
            if error:
                raise GraphError(error)
            return ids or []

        monkeypatch.setattr("leyline.web.auth.fetch_group_ids", fake_fetch)
        return app.test_client()

    return _make


def sign_in(client):
    response = client.get("/auth/callback")
    assert response.status_code == 302, response.get_data(as_text=True)
    return client.get("/me").get_json()


def test_an_overage_user_gets_their_groups_from_graph(make_client, db):
    calls = []
    client = make_client(
        OVERAGE_CLAIMS, ids=[HR_ID, STAFF_ID, STRANGER_ID], calls=calls
    )

    me = sign_in(client)

    assert me["groups"] == ["hr", "staff"]
    [(token, kwargs)] = calls
    assert token == "graph-access-token"
    assert (
        kwargs["base_url"] == "https://graph.microsoft.com/v1.0"
    )  # never the token's URL
    [event] = get_recent_events(db, 1, event_type="AUTH_LOGIN")
    assert event["details"] == {"group_overage": "resolved"}


def test_a_graph_failure_keeps_the_user_signed_in_with_no_extra_access(make_client, db):
    client = make_client(OVERAGE_CLAIMS, error="http_403")

    me = sign_in(client)

    assert me["groups"] == []  # fail closed: only `*` documents
    [event] = get_recent_events(db, 1, event_type="AUTH_LOGIN")
    assert event["details"] == {
        "groups_incomplete": True,
        "group_overage": "graph_http_403",
    }


def test_an_unexpected_error_in_the_lookup_also_fails_closed(
    make_client, monkeypatch, db
):
    client = make_client(OVERAGE_CLAIMS)

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("leyline.web.auth.fetch_group_ids", explode)
    me = sign_in(client)
    assert me["groups"] == []
    [event] = get_recent_events(db, 1, event_type="AUTH_LOGIN")
    assert event["details"]["group_overage"] == "graph_unexpected_error"


def test_nothing_is_looked_up_when_the_feature_is_off(make_client):
    calls = []
    client = make_client(OVERAGE_CLAIMS, resolve=False, ids=[HR_ID], calls=calls)
    assert sign_in(client)["groups"] == [] and calls == []


def test_nothing_is_looked_up_for_a_normal_token(make_client):
    calls = []
    claims = {"sub": "u-normal", "groups": [STAFF_ID]}
    client = make_client(claims, ids=[HR_ID], calls=calls)
    assert sign_in(client)["groups"] == ["staff"] and calls == []


def test_the_access_token_is_never_kept(make_client, redis, db):
    client = make_client(OVERAGE_CLAIMS, ids=[HR_ID])
    sign_in(client)

    stored = []
    for key in redis.keys("*"):
        kind = redis.type(key)
        kind = kind.decode() if isinstance(kind, bytes) else kind
        if kind == "string":
            stored.append(str(redis.get(key)))
        elif kind == "hash":
            stored.append(str(redis.hgetall(key)))
    assert stored  # the session really is in there
    assert "graph-access-token" not in " ".join(stored)
    for event in get_recent_events(db, 50):
        assert "graph-access-token" not in json.dumps(event)


def test_resolved_groups_open_documents_the_unresolved_user_cannot_read(
    make_client, add_doc, monkeypatch
):
    add_doc("Salary bands", "# Pay\n\nSalary bands are reviewed each April.", ["hr"])

    def ask(client):
        token = client.get("/me").get_json()["csrf_token"]
        return client.post(
            "/chat",
            json={"message": "salary bands"},
            headers={"X-CSRF-Token": token},
        ).get_json()

    resolved = make_client(OVERAGE_CLAIMS, ids=[HR_ID])
    sign_in(resolved)
    assert ask(resolved)["citations"]  # the HR document is readable now

    denied = make_client(OVERAGE_CLAIMS, error="http_403")
    sign_in(denied)
    assert not ask(denied).get("citations")  # same user, Graph down: nothing restricted


def test_the_module_never_reads_a_url_from_the_token():
    source = Path(graph_module.__file__).read_text(encoding="utf-8")
    assert "_claim_sources" not in source and "endpoint" not in source
