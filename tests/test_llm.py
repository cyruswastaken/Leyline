"""The chat model adapter, its circuit breaker, and degraded-mode chat.

The adapter is exercised against a local server that speaks the OpenAI
``/v1/chat/completions`` protocol, so the real SDK code path runs.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from leyline.adapters.circuit_breaker import CircuitBreaker
from leyline.adapters.llm_openai import OpenAICompatibleChatModel
from leyline.config import Settings
from leyline.ports.llm import ModelUnavailableError
from leyline.services import llm as prompts
from leyline.wiring import build_chat_model
from tests.support import CSRF_HEADER, PERSONAS, login_as, make_settings

# --- a fake OpenAI-compatible chat server ------------------------------------------


class FakeChatServer:
    def __init__(
        self,
        *,
        status=200,
        content="Reset it in the portal.",
        stream_pieces=None,
        drop_after=None,
        delay=0.0,
        empty=False,
    ):
        self.requests: list[dict] = []
        self.status = status
        outer = self
        pieces = stream_pieces or ["Reset ", "it ", "now."]

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
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": {"message": "secret detail"}}')
                    return
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for i, piece in enumerate(pieces):
                        if drop_after is not None and i == drop_after:
                            self.wfile.write(b"data: {not json\n\n")
                            self.wfile.flush()
                            return
                        chunk = {
                            "id": "c",
                            "object": "chat.completion.chunk",
                            "created": 0,
                            "model": "m",
                            "choices": [{"index": 0, "delta": {"content": piece}}],
                        }
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                payload = json.dumps(
                    {
                        "id": "c",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "m",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": "" if empty else content,
                                },
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # clients that cancel a stream drop the connection

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}/v1"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def chat_server():
    servers = []

    def make(**kwargs):
        server = FakeChatServer(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def model_for(server, **kwargs):
    kwargs.setdefault("max_retries", 0)
    return OpenAICompatibleChatModel(
        base_url=server.url, api_key="k", model="chat-1", **kwargs
    )


# --- adapter ------------------------------------------------------------------------


def test_complete_returns_the_text_and_sends_both_prompts(chat_server):
    server = chat_server()
    answer = model_for(server).complete("SYSTEM", "USER")
    assert answer == "Reset it in the portal."
    request = server.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["auth"] == "Bearer k"
    assert request["body"]["model"] == "chat-1"
    assert request["body"]["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]
    assert "tools" not in request["body"]  # the model is never given tools


def test_temperature_is_sent_by_default_and_can_be_left_out(chat_server):
    server = chat_server()
    model_for(server).complete("s", "u")
    assert server.requests[0]["body"]["temperature"] == 0.1

    quiet = chat_server()
    model_for(quiet, temperature=None).complete("s", "u")
    assert "temperature" not in quiet.requests[0]["body"]

    streamed = chat_server(stream_pieces=["a"])
    list(model_for(streamed, temperature=None).stream("s", "u"))
    assert "temperature" not in streamed.requests[0]["body"]


def test_a_missing_key_still_works_for_local_servers(chat_server):
    server = chat_server()
    OpenAICompatibleChatModel(
        base_url=server.url, api_key="", model="m", max_retries=0
    ).complete("s", "u")
    assert server.requests[0]["auth"] == "Bearer not-needed"


def test_stream_yields_pieces_in_order(chat_server):
    server = chat_server(stream_pieces=["a", "b", "c"])
    pieces = list(model_for(server).stream("s", "u"))
    assert pieces == ["a", "b", "c"]
    assert server.requests[0]["body"]["stream"] is True


def test_a_server_error_becomes_model_unavailable_without_leaking_detail(chat_server):
    server = chat_server(status=500)
    with pytest.raises(ModelUnavailableError) as caught:
        model_for(server).complete("s", "u")
    assert caught.value.reason == "http_500"
    assert "secret detail" not in str(caught.value)


def test_5xx_is_retried_by_the_sdk_up_to_the_limit(chat_server):
    server = chat_server(status=503)
    with pytest.raises(ModelUnavailableError):
        model_for(server, max_retries=2).complete("s", "u")
    assert len(server.requests) == 3


def test_a_timeout_is_reported_as_one(chat_server):
    server = chat_server(delay=1.0)
    with pytest.raises(ModelUnavailableError) as caught:
        model_for(server, timeout_seconds=0.2).complete("s", "u")
    assert caught.value.reason == "timeout"


def test_a_refused_connection_is_reported_as_one():
    model = OpenAICompatibleChatModel(
        base_url="http://127.0.0.1:9/v1", api_key="", model="m", max_retries=0
    )
    with pytest.raises(ModelUnavailableError) as caught:
        model.complete("s", "u")
    assert caught.value.reason == "connection_error"


def test_an_empty_answer_is_a_failure_not_a_blank_reply(chat_server):
    server = chat_server(empty=True)
    with pytest.raises(ModelUnavailableError) as caught:
        model_for(server).complete("s", "u")
    assert caught.value.reason == "empty_response"


def test_a_stream_that_breaks_midway_raises_after_the_pieces_already_sent(chat_server):
    server = chat_server(stream_pieces=["a", "b", "c"], drop_after=1)
    received = []
    with pytest.raises(ModelUnavailableError):
        for piece in model_for(server).stream("s", "u"):
            received.append(piece)
    assert received == ["a"]


def test_closing_a_stream_early_is_not_counted_as_a_failure(chat_server):
    server = chat_server(stream_pieces=["a", "b", "c"])
    breaker = CircuitBreaker(failure_threshold=1)
    stream = model_for(server, breaker=breaker).stream("s", "u")
    assert next(stream) == "a"
    stream.close()
    assert breaker.allow() is True


# --- circuit breaker ----------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_circuit_opens_after_consecutive_failures_and_recovers():
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=10, clock=clock)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.allow() is True  # below the threshold
    breaker.record_failure()
    assert breaker.is_open and breaker.allow() is False

    clock.now = 9.9
    assert breaker.allow() is False
    clock.now = 10.0
    assert breaker.allow() is True  # one trial call
    assert breaker.allow() is False  # others still refused during the trial
    breaker.record_success()
    assert not breaker.is_open and breaker.allow() is True


def test_a_failed_trial_reopens_the_circuit_for_a_full_period():
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)
    breaker.record_failure()
    clock.now = 10.0
    assert breaker.allow() is True
    breaker.record_failure()
    clock.now = 19.0
    assert breaker.allow() is False
    clock.now = 20.0
    assert breaker.allow() is True


def test_a_success_resets_the_failure_count():
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.allow() is True


def test_an_open_circuit_refuses_without_calling_the_model(chat_server):
    server = chat_server(status=500)
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)
    model = model_for(server, breaker=breaker)
    for _ in range(2):
        with pytest.raises(ModelUnavailableError):
            model.complete("s", "u")
    assert len(server.requests) == 2
    with pytest.raises(ModelUnavailableError) as caught:
        model.complete("s", "u")
    with pytest.raises(ModelUnavailableError):
        list(model.stream("s", "u"))
    assert caught.value.reason == "circuit_open"
    assert len(server.requests) == 2  # nothing further reached the server


# --- configuration ------------------------------------------------------------------


def test_llm_settings_take_precedence_over_the_azure_trio():
    settings = Settings(
        _env_file=None,
        llm_base_url="http://localhost:11434/v1",
        llm_model="qwen2.5:3b",
        azure_openai_endpoint="https://a.invalid/",
        azure_openai_api_key="k",
        azure_openai_deployment="d",
    )
    assert settings.llm_endpoint == ("http://localhost:11434/v1", "", "qwen2.5:3b")


def test_the_azure_trio_maps_to_the_v1_url():
    settings = Settings(
        _env_file=None,
        azure_openai_endpoint="https://a.invalid/",
        azure_openai_api_key="k",
        azure_openai_deployment="d",
    )
    assert settings.llm_endpoint == ("https://a.invalid/openai/v1/", "k", "d")


def test_nothing_configured_means_no_model():
    settings = Settings(_env_file=None)
    assert settings.llm_configured is False
    assert build_chat_model(settings) is None


def test_production_accepts_the_generic_llm_settings():
    settings = Settings(
        _env_file=None,
        leyline_env="production",
        llm_base_url="https://llm.invalid/v1",
        llm_model="m",
        oidc_metadata_url="https://idp.invalid/.well-known/openid-configuration",
        oidc_client_id="c",
        oidc_client_secret="s",
        session_cookie_secure=True,
        database_url="postgresql+pg8000://u:p@db.invalid:5432/leyline",
        blob_connection_string="AccountName=x;AccountKey=y",
    )
    assert settings.llm_configured


# --- prompt construction -------------------------------------------------------------


def test_generate_answer_sends_the_grounding_rules_and_the_context():
    seen = {}

    class Recorder:
        model = "r"

        def complete(self, system, user):
            seen["system"], seen["user"] = system, user
            return "ok"

        def stream(self, system, user):
            yield "ok"

    assert prompts.generate_answer("Q?", "CTX", "Src A", model=Recorder()) == "ok"
    assert "ONLY" in seen["system"]
    assert "CTX" in seen["user"] and "Q?" in seen["user"] and "Src A" in seen["user"]


# --- degraded mode through the chat route --------------------------------------------


@pytest.fixture
def failing_model(monkeypatch):
    def broken(*args, **kwargs):
        raise ModelUnavailableError("http_500")

    monkeypatch.setattr("leyline.services.chat_service.generate_answer", broken)


def ask(client, persona, message="How do I reset my password?"):
    token = login_as(client, PERSONAS[persona])
    return client.post("/chat", json={"message": message}, headers={CSRF_HEADER: token})


def test_a_model_outage_answers_503_with_a_clear_message_and_logs_it(
    db_client, add_doc, failing_model, events
):
    add_doc(
        "Password reset", "Reset your password in the self-service portal.", ["staff"]
    )
    response = ask(db_client, "employee")

    assert response.status_code == 503
    body = response.get_json()
    assert body["reason"] == "model_unavailable"
    assert "temporarily unavailable" in body["error"]
    assert "http_500" not in json.dumps(body)  # no internals to the client
    errors = [e for e in events if e["event_type"] == "MODEL_ERROR"]
    assert errors and errors[0]["reason"] == "http_500"
    assert errors[0]["user_id"] == PERSONAS["employee"].user_id
    assert not [e for e in events if e["event_type"] == "NORMAL_REQUEST"]


def test_without_a_configured_model_chat_degrades_the_same_way(
    redis, db, blobs, embedder, add_doc, events
):
    from leyline.app import create_app

    add_doc(
        "Password reset", "Reset your password in the self-service portal.", ["staff"]
    )
    app = create_app(
        make_settings(azure_openai_endpoint=None),
        redis_client=redis,
        database=db,
        blob_store=blobs,
        embedder=embedder,
    )
    response = ask(app.test_client(), "employee")
    assert response.status_code == 503
    assert response.get_json()["reason"] == "model_unavailable"
    assert [e["reason"] for e in events if e["event_type"] == "MODEL_ERROR"] == [
        "not_configured"
    ]


def test_questions_with_no_readable_document_do_not_need_the_model(
    db_client, add_doc, failing_model
):
    """A refusal is not a model outage: nothing to generate, nothing to fail."""
    add_doc("Secret", "Quarterly hunting rota for the security team.", ["security"])
    response = ask(db_client, "employee", "hunting rota")
    assert response.status_code == 200
    assert "couldn't find" in response.get_json()["response"]
