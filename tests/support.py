import hashlib
import os
import tempfile
from collections.abc import Sequence
from typing import Any

# A few modules read configuration when imported, so this must be set first.
# No real model or identity-provider calls are made in tests.
os.environ["LEYLINE_ENV"] = "test"
os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="leyline-test-logs-")

from leyline.auth.principal import AppRole, Principal
from leyline.config import Settings
from leyline.ports.search import SearchHit

CSRF_HEADER = "X-CSRF-Token"

# Integration tests need a real Postgres with the pgvector extension:
#   docker compose up -d postgres
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+pg8000://leyline:leyline-dev-only@localhost:5432/leyline_test",
)

PERSONAS: dict[str, Principal] = {
    "employee": Principal(
        user_id="u-employee",
        name="Eve Employee",
        groups=frozenset({"staff"}),
    ),
    "security": Principal(
        user_id="u-security",
        name="Sam Security",
        groups=frozenset({"staff", "security"}),
    ),
    "auditor": Principal(
        user_id="u-auditor",
        name="Ada Auditor",
        groups=frozenset({"staff"}),
        roles=frozenset({AppRole.USER, AppRole.SECURITY_AUDITOR}),
    ),
    "kb_admin": Principal(
        user_id="u-kbadmin",
        name="Kim Admin",
        groups=frozenset({"staff"}),
        roles=frozenset({AppRole.USER, AppRole.KB_ADMIN}),
    ),
    "support_agent": Principal(
        user_id="u-agent",
        name="Sue Agent",
        groups=frozenset({"staff"}),
        roles=frozenset({AppRole.USER, AppRole.SUPPORT_AGENT}),
    ),
}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "leyline_env": "test",
        "azure_openai_endpoint": "https://example.invalid/",
        "azure_openai_api_key": "test-key",
        "azure_openai_deployment": "test-deployment",
        "oidc_metadata_url": "https://idp.invalid/.well-known/openid-configuration",
        "oidc_client_id": "leyline-web",
        "oidc_client_secret": "test-secret",
        "log_dir": os.environ["LOG_DIR"],
        "rate_limit_ip_requests": 1000,
        # Retried jobs become runnable immediately unless a test says otherwise.
        "ingest_backoff_seconds": 0,
    }
    values.update(overrides)
    # _env_file=None: never let a developer's local .env change test behavior.
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def login_as(client, principal: Principal) -> str:
    """Create a real server-side session for ``principal``; return its CSRF token."""
    token = f"csrf-{principal.user_id}"
    with client.session_transaction() as session:
        session["principal"] = principal.to_session()
        session["csrf_token"] = token
    return token


# --- fakes -----------------------------------------------------------------


class FakeEmbedder:
    """Deterministic 8-dimension embeddings, with injectable failures."""

    dimensions = 8

    def __init__(
        self,
        model: str = "fake-embed-1",
        *,
        fail_times: int = 0,
        error: Exception | None = None,
    ) -> None:
        self.model = model
        self.calls: list[list[str]] = []
        self.failures_left = fail_times
        self._error = error or RuntimeError("embedding provider unavailable")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.failures_left > 0:
            self.failures_left -= 1
            raise self._error
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [(byte / 127.5) - 1.0 for byte in digest[: self.dimensions]]


class FakeSearchIndex:
    """Records who searched for what; returns whatever the test configured."""

    def __init__(self) -> None:
        self.hits: list[SearchHit] = []
        self.calls: list[tuple[Principal, str, int]] = []

    def search(
        self, principal: Principal, query: str, *, limit: int
    ) -> list[SearchHit]:
        self.calls.append((principal, query, limit))
        return list(self.hits)


class StubDatabase:
    """For hermetic tests: any use of the database is a test bug."""

    def session(self) -> Any:
        raise AssertionError("a hermetic test touched the database")

    def ping(self) -> bool:
        raise AssertionError("a hermetic test touched the database")

    @property
    def engine(self) -> Any:
        raise AssertionError("a hermetic test touched the database")


def person(
    user_id: str, *groups: str, roles: frozenset[AppRole] | None = None
) -> Principal:
    """A principal with exactly these document groups (and optional app roles)."""
    return Principal(
        user_id=user_id,
        name=user_id,
        groups=frozenset(groups),
        roles=roles or frozenset({AppRole.USER}),
    )
