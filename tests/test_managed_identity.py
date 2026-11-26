"""Signing in to Azure services with the container's managed identity, not keys."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from leyline import wiring
from leyline.adapters.blob_azure import AzureBlobStore
from leyline.adapters.redis_entra import EntraRedisCredentials
from leyline.auth.azure_identity import (
    OPENAI,
    POSTGRES,
    REDIS,
    STORAGE,
    IdentityError,
    ManagedIdentity,
)
from leyline.config import Settings
from leyline.db.database import Database

# --- a local stand-in for the container's identity endpoint ------------------------------


class FakeIdentityEndpoint:
    def __init__(self, *, status=200, reply=None):
        self.requests = []
        self.status = status
        self.expires_in = 3600
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import parse_qs, urlsplit

                parts = urlsplit(self.path)
                outer.requests.append(
                    {
                        "query": {k: v[0] for k, v in parse_qs(parts.query).items()},
                        "header": self.headers.get("X-IDENTITY-HEADER"),
                    }
                )
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b'{"error": "secret detail"}')
                    return
                n = len(outer.requests)
                body = reply or {
                    "access_token": f"token-{n}",
                    "expires_on": str(int(outer.now() + outer.expires_in)),
                }
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.now = lambda: 1_000_000.0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}/msi/token"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def endpoint():
    servers = []

    def make(**kwargs):
        server = FakeIdentityEndpoint(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def identity(server, **kwargs):
    return ManagedIdentity(
        kwargs.pop("client_id", None),
        endpoint=server.url,
        header="the-secret-header",
        now=server.now,
        **kwargs,
    )


# --- the token client ----------------------------------------------------------------------


def test_a_token_is_requested_for_the_resource_with_the_identity_header(endpoint):
    server = endpoint()
    token = identity(server, client_id="client-123").token(POSTGRES)

    assert token.token == "token-1"
    request = server.requests[0]
    assert request["header"] == "the-secret-header"
    assert request["query"] == {
        "api-version": "2019-08-01",
        "resource": POSTGRES,
        "client_id": "client-123",
    }


def test_no_client_id_is_sent_for_a_system_assigned_identity(endpoint):
    server = endpoint()
    identity(server).token(STORAGE)
    assert "client_id" not in server.requests[0]["query"]


def test_a_fresh_token_is_reused_and_each_resource_has_its_own(endpoint):
    server = endpoint()
    ident = identity(server)
    assert ident.token(POSTGRES).token == ident.token(POSTGRES).token == "token-1"
    assert ident.token(OPENAI).token == "token-2"
    assert len(server.requests) == 2


def test_a_token_close_to_expiry_is_replaced(endpoint):
    server = endpoint()
    ident = identity(server)
    server.expires_in = 200  # inside the five-minute margin straight away
    first = ident.token(POSTGRES)
    server.expires_in = 3600
    second = ident.token(POSTGRES)
    assert first.token == "token-1" and second.token == "token-2"


def test_the_sdk_shape_asks_for_the_resource_behind_the_scope(endpoint):
    server = endpoint()
    token = identity(server).get_token(STORAGE + "/.default")
    assert server.requests[0]["query"]["resource"] == STORAGE
    assert token.token == "token-1" and token.expires_on > 1_000_000


def test_a_scope_is_required(endpoint):
    with pytest.raises(IdentityError) as caught:
        identity(endpoint()).get_token()
    assert caught.value.reason == "no_scope"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"status": 401}, "http_401"),
        ({"status": 500}, "http_500"),
        ({"reply": {"nothing": "useful"}}, "bad_response"),
        ({"reply": {"access_token": "t", "expires_on": "soon"}}, "bad_response"),
    ],
)
def test_a_failure_is_an_identity_error_without_leaking_detail(
    endpoint, kwargs, reason
):
    with pytest.raises(IdentityError) as caught:
        identity(endpoint(**kwargs)).token(POSTGRES)
    assert caught.value.reason == reason
    assert "secret detail" not in str(caught.value)


def test_outside_azure_there_is_no_identity(monkeypatch):
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)
    with pytest.raises(IdentityError) as caught:
        ManagedIdentity().token(POSTGRES)
    assert caught.value.reason == "not_in_azure"


def test_an_unreachable_endpoint_is_reported():
    ident = ManagedIdentity(
        endpoint="http://127.0.0.1:9/token", header="h", timeout_seconds=1
    )
    with pytest.raises(IdentityError) as caught:
        ident.token(POSTGRES)
    assert caught.value.reason == "connection_error"


# --- each service ---------------------------------------------------------------------------


def test_the_database_asks_for_a_new_token_for_every_connection(db):
    calls = []

    def provider():
        calls.append(1)
        return "leyline-dev-only"  # the dev password stands in for a token

    url = db.engine.url.set(password=None).render_as_string(hide_password=False)
    database = Database(url, pool_size=2, password_provider=provider)
    try:
        assert database.ping()
        with database.session() as session:
            assert session.execute(text("select 1")).scalar() == 1
        first = len(calls)
        assert first >= 1
        # Two connections open at once need two sign-ins.
        with database.connection(), database.connection():
            pass
        assert len(calls) > first
    finally:
        database.dispose()


def test_a_database_that_cannot_get_a_token_is_not_ready():
    def broken():
        raise IdentityError("not_in_azure")

    url = "postgresql+pg8000://leyline@127.0.0.1:5432/leyline"
    database = Database(url, password_provider=broken)
    try:
        with pytest.raises(Exception) as caught:
            database.ping()
        assert "not_in_azure" in str(caught.value)
    finally:
        database.dispose()


def test_redis_gets_the_identitys_object_id_and_a_token(endpoint):
    server = endpoint()
    provider = EntraRedisCredentials(identity(server), "object-id-1")
    assert provider.get_credentials() == ("object-id-1", "token-1")
    assert server.requests[0]["query"]["resource"] == REDIS


def test_the_redis_client_uses_the_provider_instead_of_a_password(monkeypatch):
    settings = Settings(
        _env_file=None,
        redis_url="rediss://cache.example:10000/0",
        redis_entra_username="object-id-1",
    )
    client = wiring.build_redis(settings)
    kwargs = client.connection_pool.connection_kwargs
    assert isinstance(kwargs["credential_provider"], EntraRedisCredentials)
    assert "password" not in kwargs or not kwargs["password"]


def test_without_the_setting_redis_keeps_its_password_in_the_url():
    client = wiring.build_redis(
        Settings(_env_file=None, redis_url="redis://:secret@cache.example:6379/0")
    )
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs.get("password") == "secret" and "credential_provider" not in kwargs


def test_blob_storage_signs_in_with_the_identity_when_given_an_account_address(
    endpoint,
):
    store = AzureBlobStore(
        None,
        "documents",
        account_url="https://account.blob.core.windows.net",
        credential=identity(endpoint()),
    )
    assert store is not None


def test_blob_storage_needs_a_connection_string_or_an_address_and_credential():
    with pytest.raises(ValueError, match="connection string"):
        AzureBlobStore(None, "documents")
    with pytest.raises(ValueError, match="connection string"):
        AzureBlobStore(None, "documents", account_url="https://a.blob.core.windows.net")


def test_the_model_clients_send_a_fresh_token_instead_of_a_key(endpoint, monkeypatch):
    from tests.test_llm import FakeChatServer

    chat = FakeChatServer()
    try:
        from leyline.adapters.llm_openai import OpenAICompatibleChatModel

        tokens = iter(["tok-a", "tok-b"])
        model = OpenAICompatibleChatModel(
            base_url=chat.url,
            api_key=lambda: next(tokens),
            model="m",
            max_retries=0,
        )
        model.complete("s", "u")
        model.complete("s", "u")
        assert [r["auth"] for r in chat.requests] == ["Bearer tok-a", "Bearer tok-b"]
    finally:
        chat.close()


def test_the_wiring_uses_the_identity_for_each_flag(endpoint, monkeypatch):
    server = endpoint()
    monkeypatch.setenv("IDENTITY_ENDPOINT", server.url)
    monkeypatch.setenv("IDENTITY_HEADER", "h")
    settings = Settings(
        _env_file=None,
        llm_base_url="https://x.openai.azure.com/openai/v1/",
        llm_model="chat",
        llm_entra_auth=True,
        embedding_base_url="https://x.openai.azure.com/openai/v1/",
        embedding_model="embed",
        embedding_entra_auth=True,
    )
    assert wiring.build_chat_model(settings) is not None
    assert wiring.build_embedder(settings) is not None
    provider = wiring._token_provider(settings, OPENAI)
    assert provider().startswith("token-")
    assert server.requests[-1]["query"]["resource"] == OPENAI


# --- configuration ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "blob_account_url": "https://a.blob.core.windows.net",
                "blob_connection_string": "UseDevelopmentStorage=true",
            },
            "not both",
        ),
        ({"blob_account_url": "http://a.blob.core.windows.net"}, "https"),
        ({"database_entra_auth": True}, "needs DATABASE_URL"),
        (
            {
                "database_entra_auth": True,
                "database_url": "postgresql+pg8000://u:pw@h:5432/d",
            },
            "remove the password",
        ),
        (
            {"redis_entra_username": "oid", "redis_url": "rediss://:secret@h:10000/0"},
            "remove the password",
        ),
        ({"llm_entra_auth": True, "llm_api_key": "k"}, "mutually exclusive"),
        (
            {"embedding_entra_auth": True, "embedding_api_key": "k"},
            "mutually exclusive",
        ),
    ],
)
def test_an_unsafe_identity_configuration_is_refused(overrides, message):
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **overrides)


def test_a_complete_identity_configuration_is_accepted_and_counts_as_configured():
    s = Settings(
        _env_file=None,
        database_url="postgresql+pg8000://identity-name@h:5432/d",
        database_entra_auth=True,
        blob_account_url="https://a.blob.core.windows.net",
        redis_url="rediss://h:10000/0",
        redis_entra_username="oid",
        llm_entra_auth=True,
        embedding_entra_auth=True,
    )
    assert s.uses_managed_identity and s.blob_configured and s.database_configured


def test_by_default_no_identity_is_used():
    assert Settings(_env_file=None).uses_managed_identity is False
