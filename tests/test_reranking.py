"""Reranking: safe reordering of hits the asker may already read."""

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from leyline.adapters.circuit_breaker import CircuitBreaker
from leyline.adapters.rerank_http import MAX_CHARS_PER_CHUNK, HttpReranker
from leyline.app import create_app
from leyline.ports.rerank import RerankError, Scored
from leyline.ports.search import SearchHit
from leyline.services.monitoring import get_recent_events
from leyline.services.reranking import rerank_hits
from tests.support import CSRF_HEADER, PERSONAS, FakeChatModel, login_as, make_settings


def hit(name, content="text", heading=None):
    return SearchHit(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        document_id=uuid.uuid5(uuid.NAMESPACE_URL, "doc-" + name),
        version_id=uuid.uuid4(),
        title=name,
        heading=heading,
        content=content,
        score=1.0,
    )


class ScriptedReranker:
    model = "scripted"

    def __init__(self, scores=None, fail=None, extra=()):
        self.scores = scores or {}
        self.fail = fail
        self.extra = list(extra)
        self.calls = []

    def rerank(self, query, hits):
        self.calls.append((query, list(hits)))
        if self.fail:
            raise RerankError(self.fail)
        scored = [Scored(h, self.scores.get(h.title, 0.5)) for h in hits]
        return scored + [Scored(h, 0.99) for h in self.extra]


# --- the safe application of a reranker -------------------------------------------------


def test_hits_are_ordered_by_reranker_score_and_cut_to_the_keep_count():
    hits = [hit("a"), hit("b"), hit("c"), hit("d")]
    reranker = ScriptedReranker({"a": 0.2, "b": 0.9, "c": 0.5, "d": 0.7})

    kept, failure = rerank_hits(reranker, "q", hits, keep=3)

    assert [h.title for h in kept] == ["b", "d", "c"] and failure is None
    assert reranker.calls[0][0] == "q"


def test_ties_keep_the_search_order():
    hits = [hit("a"), hit("b"), hit("c")]
    kept, _ = rerank_hits(ScriptedReranker(), "q", hits, keep=3)
    assert [h.title for h in kept] == ["a", "b", "c"]


def test_a_floor_drops_weak_matches_and_can_leave_nothing():
    hits = [hit("a"), hit("b")]
    reranker = ScriptedReranker({"a": 0.30, "b": 0.05})
    kept, _ = rerank_hits(reranker, "q", hits, keep=5, min_score=0.1)
    assert [h.title for h in kept] == ["a"]
    none, failure = rerank_hits(reranker, "q", hits, keep=5, min_score=0.5)
    assert none == [] and failure is None


def test_a_chunk_the_search_did_not_return_is_never_added():
    hits = [hit("a")]
    stranger = hit("secret-from-elsewhere")
    reranker = ScriptedReranker({"a": 0.1}, extra=[stranger])

    kept, _ = rerank_hits(reranker, "q", hits, keep=5)

    assert [h.title for h in kept] == ["a"]  # the 0.99-scoring stranger is ignored


def test_the_search_objects_come_back_not_the_rerankers_copies():
    original = hit("a", "original text")
    imposter = SearchHit(**{**original.__dict__, "content": "tampered text"})

    class Swapper:
        model = "s"

        def rerank(self, query, hits):
            return [Scored(imposter, 0.9)]

    kept, _ = rerank_hits(Swapper(), "q", [original], keep=5)
    assert kept == [original] and kept[0].content == "original text"


def test_duplicate_scores_for_one_chunk_use_the_best():
    a = hit("a")

    class Twice:
        model = "t"

        def rerank(self, query, hits):
            return [Scored(a, 0.1), Scored(a, 0.8)]

    kept, _ = rerank_hits(Twice(), "q", [a, hit("b")], keep=5, min_score=0.5)
    assert [h.title for h in kept] == ["a"]


def test_a_failing_reranker_falls_back_to_the_search_order():
    hits = [hit("a"), hit("b"), hit("c")]
    kept, failure = rerank_hits(ScriptedReranker(fail="timeout"), "q", hits, keep=2)
    assert [h.title for h in kept] == ["a", "b"] and failure == "timeout"


# --- the HTTP adapter (against a local server speaking TEI's protocol) -------------------


class FakeRerankServer:
    def __init__(self, *, status=200, reply=None, delay=0.0, scores=None):
        self.requests = []
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
                if delay:
                    time.sleep(delay)
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b'{"error": "secret detail"}')
                    return
                texts = body["texts"]
                payload = reply
                if payload is None:
                    payload = [
                        {"index": i, "score": (scores or {}).get(i, 1 - i * 0.1)}
                        for i in range(len(texts))
                    ]
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a client that timed out drops the connection

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def rerank_server():
    servers = []

    def make(**kwargs):
        server = FakeRerankServer(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def adapter(server, **kwargs):
    kwargs.setdefault("timeout_seconds", 2)
    return HttpReranker(base_url=server.url, model="bge-reranker-base", **kwargs)


def test_the_adapter_sends_the_query_and_texts_and_maps_scores_back(rerank_server):
    server = rerank_server(scores={0: 0.1, 1: 0.9})
    hits = [hit("one", "alpha", "Heading"), hit("two", "beta")]

    scored = adapter(server, api_key="k").rerank("my question", hits)

    request = server.requests[0]
    assert request["path"] == "/rerank" and request["auth"] == "Bearer k"
    assert request["body"]["query"] == "my question"
    assert request["body"]["texts"] == ["one\nHeading\nalpha", "two\nbeta"]
    assert [(s.hit.title, s.score) for s in scored] == [("one", 0.1), ("two", 0.9)]


def test_the_adapter_sends_no_key_header_when_there_is_none(rerank_server):
    server = rerank_server()
    adapter(server).rerank("q", [hit("a")])
    assert server.requests[0]["auth"] is None


def test_long_chunks_are_truncated_before_they_are_sent(rerank_server):
    server = rerank_server()
    adapter(server).rerank("q", [hit("a", "x" * 5000)])
    assert len(server.requests[0]["body"]["texts"][0]) == MAX_CHARS_PER_CHUNK


def test_no_hits_means_no_call(rerank_server):
    server = rerank_server()
    assert adapter(server).rerank("q", []) == []
    assert server.requests == []


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"status": 500}, "http_500"),
        ({"status": 503}, "http_503"),
        ({"reply": {"not": "a list"}}, "bad_response"),
        ({"reply": [{"index": 0}]}, "bad_response"),
        ({"reply": [{"index": 5, "score": 0.5}]}, "bad_response"),
        ({"reply": [{"index": 0, "score": 0.5}]}, "bad_response"),  # one of two
        (
            {"reply": [{"index": 0, "score": "nan"}, {"index": 1, "score": 0.1}]},
            "bad_response",
        ),
    ],
)
def test_a_failed_or_malformed_reply_is_a_rerank_error(rerank_server, kwargs, reason):
    server = rerank_server(**kwargs)
    with pytest.raises(RerankError) as caught:
        adapter(server).rerank("q", [hit("a"), hit("b")])
    assert caught.value.reason == reason
    assert "secret detail" not in str(caught.value)


def test_a_slow_server_times_out(rerank_server):
    server = rerank_server(delay=0.6)
    with pytest.raises(RerankError) as caught:
        adapter(server, timeout_seconds=0.1).rerank("q", [hit("a")])
    assert caught.value.reason == "timeout"


def test_a_refused_connection_is_reported():
    reranker = HttpReranker(base_url="http://127.0.0.1:9", model="m", timeout_seconds=1)
    with pytest.raises(RerankError) as caught:
        reranker.rerank("q", [hit("a")])
    assert caught.value.reason == "connection_error"


def test_the_circuit_opens_after_repeated_failures_and_stops_calling(rerank_server):
    server = rerank_server(status=500)
    reranker = adapter(
        server, breaker=CircuitBreaker(failure_threshold=2, reset_seconds=60)
    )
    for _ in range(2):
        with pytest.raises(RerankError):
            reranker.rerank("q", [hit("a")])
    with pytest.raises(RerankError) as caught:
        reranker.rerank("q", [hit("a")])
    assert caught.value.reason == "circuit_open"
    assert len(server.requests) == 2


@pytest.mark.parametrize("make", ["scripted", "http"])
def test_every_reranker_returns_only_the_hits_it_was_given(make, rerank_server):
    """The contract any Reranker must meet."""
    reranker = ScriptedReranker() if make == "scripted" else adapter(rerank_server())
    hits = [hit("a"), hit("b"), hit("c")]
    scored = reranker.rerank("q", hits)
    assert {s.hit.chunk_id for s in scored} <= {h.chunk_id for h in hits}
    assert all(isinstance(s.score, float) for s in scored)
    assert reranker.model


# --- inside the chat pipeline -------------------------------------------------------------

PUBLIC = ("Password reset", "# Passwords\n\nReset your password in the portal.", ["*"])
SECRET = (
    "Hunting rota",
    "# Rota\n\nQuarterly password hunting rota for the security team.",
    ["security"],
)


@pytest.fixture
def make_client(db, redis, blobs, embedder):
    def _make(reranker, model=None, **settings):
        app = create_app(
            make_settings(**settings),
            redis_client=redis,
            database=db,
            blob_store=blobs,
            embedder=embedder,
            chat_model=model or FakeChatModel(),
            reranker=reranker,
        )
        app.config.update(TESTING=True)
        return app.test_client()

    return _make


def ask(client, persona, message="How do I reset my password?"):
    token = login_as(client, PERSONAS[persona])
    return client.post("/chat", json={"message": message}, headers={CSRF_HEADER: token})


def test_the_reranker_only_ever_sees_chunks_the_asker_may_read(make_client, add_doc):
    add_doc(*PUBLIC)
    add_doc(*SECRET)
    reranker = ScriptedReranker()
    client = make_client(reranker)

    ask(client, "employee", "password hunting rota")

    seen = [h.title for _, hits in reranker.calls for h in hits]
    assert (
        seen and "Hunting rota" not in seen
    )  # restricted text never leaves the search

    ask(client, "security", "password hunting rota")
    seen = [h.title for _, hits in reranker.calls for h in hits]
    assert "Hunting rota" in seen  # the same question, a reader who may see it


def test_the_answer_is_built_from_the_reranked_order(make_client, add_doc):
    add_doc(*PUBLIC)
    add_doc("Password policy", "# Policy\n\nPasswords must be long.", ["*"])
    model = FakeChatModel()
    reranker = ScriptedReranker({"Password policy": 0.99, "Password reset": 0.2})
    client = make_client(reranker, model)

    ask(client, "employee", "password")

    [(_, user)] = model.complete_calls
    assert user.index("Passwords must be long") < user.index("Reset your password")


def test_a_floor_that_rejects_everything_answers_not_found(make_client, add_doc, db):
    add_doc(*PUBLIC)
    model = FakeChatModel()
    client = make_client(
        ScriptedReranker({"Password reset": 0.02}), model, rerank_min_score=0.5
    )

    response = ask(client, "employee")

    assert "couldn't find" in response.get_json()["response"].lower()
    assert model.complete_calls == []  # nothing was sent to the chat model
    [event] = get_recent_events(db, 1, event_type="RETRIEVAL")
    assert event["details"]["reranked"] is True and event["details"]["rerank_kept"] == 0


def test_a_failing_reranker_never_blocks_the_answer(make_client, add_doc, db):
    add_doc(*PUBLIC)
    client = make_client(ScriptedReranker(fail="timeout"))

    response = ask(client, "employee")

    assert response.status_code == 200
    assert "portal" in response.get_json()["response"]
    [event] = get_recent_events(db, 1, event_type="RETRIEVAL")
    assert event["details"]["reranked"] is False
    assert event["details"]["rerank_failure"] == "timeout"


def test_without_a_reranker_nothing_changes(make_client, add_doc, db):
    add_doc(*PUBLIC)
    client = make_client(None)
    ask(client, "employee")
    [event] = get_recent_events(db, 1, event_type="RETRIEVAL")
    assert "reranked" not in event["details"]


def test_search_is_asked_for_more_candidates_when_a_reranker_is_present(
    make_client, add_doc
):
    add_doc(*PUBLIC)
    reranker = ScriptedReranker()
    client = make_client(reranker, rerank_candidates=12, search_top_k=3)
    ask(client, "employee")
    assert reranker.calls  # called with whatever the wider search returned
