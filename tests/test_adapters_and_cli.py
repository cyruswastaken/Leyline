"""Adapters against (fake or emulated) real services, the CLI, and the corpus."""

import contextlib
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from azure.core.exceptions import ResourceNotFoundError
from sqlalchemy import func, select

from leyline.adapters.blob_azure import AzureBlobStore
from leyline.adapters.blob_memory import InMemoryBlobStore
from leyline.adapters.embeddings_openai import OpenAICompatibleEmbedder
from leyline.db import __main__ as db_cli
from leyline.db import migrate
from leyline.db.models import Document
from leyline.ports.blob import BlobNotFoundError
from leyline.seed import groups_for, seed_corpus
from leyline.services.authorization import can_read, readable_by
from leyline.services.document_loader import parse_document
from tests.support import make_settings, person

REPO_CORPUS = Path(__file__).resolve().parent.parent / "corpus"

# --- embeddings adapter against a local OpenAI-compatible server ------------------


class FakeEmbeddingServer:
    """A tiny HTTP server speaking the /v1/embeddings protocol."""

    def __init__(self, *, status=200, drop_one=False, reverse=True):
        self.requests: list[dict] = []
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
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
                    self.wfile.write(b'{"error": {"message": "boom"}}')
                    return
                inputs = body["input"]
                data = [
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": [float(i), 0.5, -1.0],
                    }
                    for i in range(len(inputs))
                ]
                if reverse:
                    data.reverse()  # providers need not return them in order
                if drop_one:
                    data.pop()
                payload = json.dumps(
                    {"object": "list", "data": data, "model": "m"}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/v1"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def embedding_server():
    servers = []

    def make(**kwargs):
        server = FakeEmbeddingServer(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def embedder_for(server, **kwargs):
    return OpenAICompatibleEmbedder(
        base_url=server.url, api_key="k", model="embed-1", max_retries=0, **kwargs
    )


def test_embeddings_are_returned_in_input_order_even_if_the_provider_shuffles_them(
    embedding_server,
):
    server = embedding_server(reverse=True)
    vectors = embedder_for(server).embed(["a", "b", "c"])
    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0]
    assert server.requests[0]["path"] == "/v1/embeddings"
    assert server.requests[0]["body"]["model"] == "embed-1"
    assert server.requests[0]["body"]["input"] == ["a", "b", "c"]


def test_the_dimensions_parameter_is_sent_only_when_configured(embedding_server):
    server = embedding_server()
    embedder_for(server).embed(["a"])
    embedder_for(server, dimensions=256).embed(["a"])
    assert "dimensions" not in server.requests[0]["body"]
    assert server.requests[1]["body"]["dimensions"] == 256


def test_the_api_key_is_sent_and_a_missing_key_still_works_for_local_servers(
    embedding_server,
):
    server = embedding_server()
    embedder_for(server).embed(["a"])
    OpenAICompatibleEmbedder(
        base_url=server.url, api_key="", model="m", max_retries=0
    ).embed(["a"])
    assert server.requests[0]["auth"] == "Bearer k"
    assert server.requests[1]["auth"] == "Bearer not-needed"


def test_empty_input_makes_no_request(embedding_server):
    server = embedding_server()
    assert embedder_for(server).embed([]) == []
    assert server.requests == []


def test_a_short_response_is_an_error_not_silently_misaligned_vectors(embedding_server):
    server = embedding_server(drop_one=True)
    with pytest.raises(RuntimeError, match="2 vectors for 3 inputs"):
        embedder_for(server).embed(["a", "b", "c"])


def test_provider_errors_propagate_so_the_job_can_be_retried(embedding_server):
    server = embedding_server(status=500)
    with pytest.raises(Exception, match=r"boom|500|Internal"):
        embedder_for(server).embed(["a"])


def test_the_embedder_reports_its_model(embedding_server):
    assert embedder_for(embedding_server()).model == "embed-1"


# --- blob stores ---------------------------------------------------------------------

AZURITE = os.environ.get(
    "AZURITE_CONNECTION_STRING",
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;",
)


@pytest.fixture(params=["memory", "azurite"])
def store(request):
    """The same behavior is required of every BlobStore implementation."""
    if request.param == "memory":
        yield InMemoryBlobStore()
        return
    azure = AzureBlobStore(AZURITE, f"leyline-test-{uuid.uuid4().hex[:12]}")
    if not azure.ping():
        message = "Azurite is not reachable (docker compose up -d azurite)"
        if os.environ.get("CI") or os.environ.get("LEYLINE_REQUIRE_DB"):
            pytest.fail(message)
        pytest.skip(message)
    yield azure
    with contextlib.suppress(ResourceNotFoundError):
        azure._container.delete_container()  # absent if the test never wrote


def test_blobs_round_trip_exactly(store):
    payload = bytes(range(256)) * 40
    store.put("documents/a/b", payload, "application/octet-stream")
    assert store.get("documents/a/b") == payload


def test_putting_the_same_key_again_overwrites(store):
    store.put("k", b"first", "text/plain")
    store.put("k", b"second", "text/plain")
    assert store.get("k") == b"second"


def test_a_missing_blob_raises_a_specific_error(store):
    with pytest.raises(BlobNotFoundError):
        store.get("does/not/exist")


def test_deleting_removes_the_blob_and_is_idempotent(store):
    store.put("k", b"data", "text/plain")
    store.delete("k")
    with pytest.raises(BlobNotFoundError):
        store.get("k")
    store.delete("k")  # deleting again is not an error
    store.delete("never-existed")


def test_a_store_reports_that_it_is_reachable(store):
    assert store.ping() is True


def test_an_unreachable_azure_store_reports_not_ready_instead_of_raising():
    dead = AzureBlobStore(
        "DefaultEndpointsProtocol=http;AccountName=x;AccountKey=eA==;"
        "BlobEndpoint=http://127.0.0.1:1/x;",
        "c",
    )
    started = time.monotonic()
    assert dead.ping() is False
    # A readiness probe must answer in seconds, never wait out SDK back-off.
    assert time.monotonic() - started < 10


# --- migration CLI -------------------------------------------------------------------


def _use(monkeypatch, database):
    url = database.engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(db_cli, "get_settings", lambda: make_settings(database_url=url))


def test_the_cli_upgrades_checks_and_reports_the_revision(
    scratch_database, monkeypatch, capsys
):
    _use(monkeypatch, scratch_database)

    assert db_cli.main(["check"]) == 1  # not migrated yet
    assert "expected" in capsys.readouterr().err

    assert db_cli.main(["upgrade"]) == 0
    assert migrate.head_revision() in capsys.readouterr().out

    assert db_cli.main(["check"]) == 0
    assert db_cli.main(["current"]) == 0
    assert migrate.head_revision() in capsys.readouterr().out


def test_the_cli_can_autogenerate_a_revision_and_reports_no_drift(
    scratch_database, monkeypatch, capsys
):
    _use(monkeypatch, scratch_database)
    assert db_cli.main(["revision", "-m", "x"]) == 1  # must be migrated first
    assert db_cli.main(["upgrade"]) == 0
    assert db_cli.main(["revision"]) == 2  # a message is required
    capsys.readouterr()

    versions = Path(migrate.__file__).parent / "migrations" / "versions"
    before = set(versions.glob("*.py"))
    try:
        assert db_cli.main(["revision", "-m", "drift probe"]) == 0
        # Alembic also prints progress; our own line is the last one.
        last_line = capsys.readouterr().out.strip().splitlines()[-1]
        created = Path(last_line.removeprefix("created "))
        assert created in set(versions.glob("*.py")) - before
        body = created.read_text(encoding="utf-8")
        # models and migrations agree, so autogenerate has nothing to add
        upgrade = body.split("def upgrade()")[1].split("def downgrade()")[0]
        assert "op." not in upgrade, "models have drifted from the migrations"
    finally:
        for stray in set(versions.glob("*.py")) - before:  # never leave a migration
            stray.unlink()


def test_the_cli_fails_cleanly_without_a_database_url(monkeypatch, capsys):
    monkeypatch.setattr(
        db_cli, "get_settings", lambda: make_settings(database_url=None)
    )
    assert db_cli.main(["upgrade"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


# --- the shipped corpus --------------------------------------------------------------


def corpus_files():
    return sorted(p for p in REPO_CORPUS.rglob("*") if p.suffix in {".md", ".txt"})


def test_every_corpus_file_is_well_formed_with_a_unique_id_and_title():
    ids, titles = set(), set()
    assert len(corpus_files()) >= 49
    for path in corpus_files():
        meta = parse_document(path)
        assert meta.get("title") and meta.get("id") and meta["content"], path
        assert groups_for(meta) is not None, f"{path} has no recognised access rule"
        assert meta["id"] not in ids, f"duplicate id {meta['id']}"
        assert meta["title"] not in titles, f"duplicate title {meta['title']}"
        ids.add(meta["id"])
        titles.add(meta["title"])


@pytest.fixture
def full_corpus(ingestion, drain):
    report = seed_corpus(ingestion, REPO_CORPUS)
    drain()
    return report


def test_the_whole_corpus_imports_and_indexes_without_failures(ingestion, full_corpus):
    assert full_corpus.skipped == [] and len(full_corpus.created) >= 49
    documents = ingestion.list_documents()
    assert len(documents) == len(full_corpus.created)
    assert {d.current_version.status for d in documents} == {"indexed"}
    assert all(d.current_version.chunk_count >= 1 for d in documents)


def test_the_corpus_gives_each_kind_of_person_the_expected_amount_of_access(
    db, full_corpus
):
    people = {
        "employee": (person("p", "staff"), 33),  # 32 for everyone + the staff calendar
        "security": (person("p", "staff", "security"), 42),
        "hr": (person("p", "staff", "hr"), 37),
        "finance": (person("p", "staff", "finance"), 36),
        "no groups": (person("p"), 32),
    }
    with db.session() as session:
        for name, (principal, expected) in people.items():
            count = session.scalar(
                select(func.count()).select_from(Document).where(readable_by(principal))
            )
            assert count == expected, name


def test_database_access_matches_the_acls_declared_in_the_files(db, full_corpus):
    root = person("p", "staff", "security", "hr", "finance")
    declared = {
        parse_document(p)["title"]: groups_for(parse_document(p))
        for p in corpus_files()
    }
    for principal in (
        person("a", "staff"),
        person("b", "hr"),
        person("c", "security"),
        root,
    ):
        with db.session() as session:
            visible = set(
                session.scalars(select(Document.title).where(readable_by(principal)))
            )
        expected = {t for t, groups in declared.items() if can_read(principal, groups)}
        assert visible == expected


def test_each_document_can_be_found_by_its_own_title_by_someone_allowed_to_read_it(
    search, full_corpus
):
    root = person("p", "staff", "security", "hr", "finance")
    for path in corpus_files():
        title = parse_document(path)["title"]
        found = {h.title for h in search.search(root, title, limit=10)}
        assert title in found, f"{title!r} is not retrievable by its own title"
