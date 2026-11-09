import hashlib
import os
import re
import tempfile
from collections.abc import Sequence
from typing import Any

# A few modules read configuration when imported, so this must be set first.
# No real model or identity-provider calls are made in tests.
os.environ["LEYLINE_ENV"] = "test"
os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="leyline-test-logs-")

from leyline.auth.principal import AppRole, Principal
from leyline.config import Settings
from leyline.constants import EMBEDDING_DIMENSIONS
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
        # Loose relevance floors: most tests use hash-based fake vectors that
        # carry no meaning, so they exercise keyword behaviour.
        "search_min_similarity": 0.0,
        "search_min_lexical": 0.0,
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
    """Deterministic schema-sized embeddings, with injectable failures.

    The vectors carry no meaning (they come from a hash), so they are only good
    for exercising plumbing: storage, dimensions, ordering and failure handling.
    """

    dimensions = EMBEDDING_DIMENSIONS

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
        digest = hashlib.shake_256(text.encode()).digest(self.dimensions)
        return [(byte / 127.5) - 1.0 for byte in digest]


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


class ConceptEmbedder:
    """A tiny, controllable stand-in for a semantic model.

    Words listed under the same concept share one axis, so texts that use
    *different words* for the same idea (``stolen`` / ``took my backpack``)
    end up close together, while texts about different concepts are nearly
    orthogonal. A little deterministic noise keeps vectors non-zero. This lets
    tests exercise semantic retrieval logic without downloading a model.
    """

    model = "concept-test-model"

    def __init__(self, concepts: dict[str, set[str]]) -> None:
        self._axes = {name: i for i, name in enumerate(concepts)}
        self._words = {w: name for name, ws in concepts.items() for w in ws}
        self.calls: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.extend(texts)
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        noise = hashlib.shake_256(text.encode()).digest(EMBEDDING_DIMENSIONS)
        vector = [(b / 255.0 - 0.5) * 0.002 for b in noise]
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            concept = self._words.get(word)
            if concept is not None:
                vector[self._axes[concept]] += 1.0
        return vector


DEFAULT_CONCEPTS: dict[str, set[str]] = {
    "theft": {"lost", "stolen", "took", "taken", "backpack", "theft", "missing"},
    "baby": {"baby", "parental", "newborn", "expecting", "maternity", "infant"},
    "holiday": {"holiday", "vacation", "leave", "annual"},
    "network": {"wifi", "wireless", "network", "internet"},
    "printing": {"printer", "print", "jam", "toner"},
}
