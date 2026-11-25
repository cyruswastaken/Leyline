"""Real token counts, model, prompt version and cost on every answered turn."""

import json

import pytest

from leyline.app import create_app
from leyline.services.llm import PROMPT_VERSION, REWRITE_PROMPT, SYSTEM_PROMPT
from leyline.services.monitoring import get_recent_events
from tests.support import CSRF_HEADER, PERSONAS, FakeChatModel, login_as, make_settings

DOC = (
    "Password reset",
    "# Passwords\n\nReset your password in the self-service portal at the helpdesk.",
    ["*"],
)


@pytest.fixture
def make_client(db, redis, blobs, embedder):
    def _make(chat_model, **settings):
        app = create_app(
            make_settings(**settings),
            redis_client=redis,
            database=db,
            blob_store=blobs,
            embedder=embedder,
            chat_model=chat_model,
        )
        app.config.update(TESTING=True)
        return app.test_client()

    return _make


def stream(client, message, conversation_id=None):
    token = login_as(client, PERSONAS["employee"])
    response = client.post(
        "/api/chat/stream",
        json={"message": message, "conversation_id": conversation_id},
        headers={CSRF_HEADER: token},
    )
    blocks = response.get_data(as_text=True).strip().split("\n\n")
    events = []
    for block in blocks:
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def last_request_details(db):
    [event] = get_recent_events(db, 1, event_type="NORMAL_REQUEST")
    return event["details"]


def test_an_answered_turn_records_the_providers_token_counts_and_cost(
    make_client, add_doc, db
):
    add_doc(*DOC)
    model = FakeChatModel(reported_usage=(1000, 200))
    client = make_client(
        model, llm_price_input_per_million=0.5, llm_price_output_per_million=2.0
    )

    stream(client, "How do I reset my password?")

    details = last_request_details(db)
    assert details["token_source"] == "provider"
    assert (details["prompt_tokens"], details["completion_tokens"]) == (1000, 200)
    assert details["model"] == "fake-chat"
    assert details["prompt_version"] == PROMPT_VERSION
    assert details["cost_usd"] == pytest.approx(0.0009)  # 1000*0.5e-6 + 200*2e-6
    assert details["latency_ms"] >= 0


def test_a_follow_up_adds_the_rewrite_calls_tokens(make_client, add_doc, db):
    add_doc(*DOC)
    model = FakeChatModel(reported_usage=(100, 10), rewrite="reset my password")
    client = make_client(model)
    first = stream(client, "How do I reset my password?")
    conversation_id = first[0][1]["conversation_id"]

    stream(client, "and where is that?", conversation_id)  # needs a rewrite

    details = last_request_details(db)
    assert (details["prompt_tokens"], details["completion_tokens"]) == (200, 20)


def test_a_standalone_follow_up_costs_only_the_answer(make_client, add_doc, db):
    add_doc(*DOC)
    model = FakeChatModel(reported_usage=(100, 10))
    client = make_client(model)
    first = stream(client, "How do I reset my password?")

    stream(
        client,
        "How do I get a guest into the building?",
        first[0][1]["conversation_id"],
    )

    assert model.complete_calls == []
    assert last_request_details(db)["prompt_tokens"] == 100


def test_a_model_that_reports_nothing_falls_back_to_estimates_and_says_so(
    make_client, add_doc, db
):
    add_doc(*DOC)
    client = make_client(FakeChatModel())

    stream(client, "How do I reset my password?")

    details = last_request_details(db)
    assert details["token_source"] == "estimate"
    assert details["prompt_tokens"] > 0 and details["completion_tokens"] > 0
    assert details["cost_usd"] == 0  # no prices configured


def test_the_stored_usage_holds_numbers_and_names_but_no_text(make_client, add_doc, db):
    add_doc(*DOC)
    client = make_client(FakeChatModel(reported_usage=(10, 5)))
    stream(client, "How do I reset my password pineapplesecret?")

    details = last_request_details(db)
    assert set(details) == {
        "model",
        "prompt_version",
        "prompt_tokens",
        "completion_tokens",
        "token_source",
        "cost_usd",
        "latency_ms",
        "first_token_ms",  # streamed turns only
    }
    assert "pineapplesecret" not in json.dumps(details)


def test_a_streamed_turn_records_the_time_to_first_token_a_blocking_one_does_not(
    make_client, add_doc, db
):
    add_doc(*DOC)
    client = make_client(FakeChatModel(reported_usage=(10, 5)))
    stream(client, "How do I reset my password?")
    streamed = last_request_details(db)
    assert 0 <= streamed["first_token_ms"] <= streamed["latency_ms"]

    token = login_as(client, PERSONAS["employee"])
    client.post(
        "/chat",
        json={"message": "How do I reset my password?"},
        headers={CSRF_HEADER: token},
    )
    assert "first_token_ms" not in last_request_details(db)


def test_the_blocking_endpoint_records_usage_too(make_client, add_doc, db):
    add_doc(*DOC)
    client = make_client(FakeChatModel(reported_usage=(50, 5)))
    token = login_as(client, PERSONAS["employee"])
    client.post(
        "/chat",
        json={"message": "How do I reset my password?"},
        headers={CSRF_HEADER: token},
    )
    assert last_request_details(db)["prompt_tokens"] == 50


def test_the_auditor_sees_tokens_cost_and_the_no_answer_rate(make_client, add_doc, db):
    add_doc(*DOC)
    client = make_client(
        FakeChatModel(reported_usage=(1000, 200)),
        llm_price_input_per_million=1.0,
        llm_price_output_per_million=3.0,
    )
    stream(client, "How do I reset my password?")
    stream(client, "How do I reset my password again?")
    stream(client, "zxqv wibble frobnicate")  # nothing readable matches

    login_as(client, PERSONAS["auditor"])
    usage = client.get("/monitoring/summary").get_json()["usage"]

    [today] = usage["by_day"]
    assert today["answers"] == 2
    assert today["prompt_tokens"] == 2000 and today["completion_tokens"] == 400
    assert today["cost_usd"] == pytest.approx(0.0032)
    assert usage["days"] == 7
    assert 0.3 < usage["no_answer_rate"] < 0.4  # one of three retrievals found nothing


def test_the_usage_summary_is_empty_without_answers(client_without_turns):
    usage = client_without_turns.get("/monitoring/summary").get_json()["usage"]
    assert usage == {"days": 7, "by_day": [], "no_answer_rate": 0.0}


@pytest.fixture
def client_without_turns(make_client):
    client = make_client(FakeChatModel())
    login_as(client, PERSONAS["auditor"])
    return client


def test_other_roles_cannot_read_the_usage_summary(make_client):
    client = make_client(FakeChatModel())
    login_as(client, PERSONAS["employee"])
    assert client.get("/monitoring/summary").status_code == 403


def test_the_prompt_version_changes_when_a_prompt_changes():
    import hashlib

    assert (
        hashlib.sha256((SYSTEM_PROMPT + REWRITE_PROMPT).encode("utf-8")).hexdigest()[
            :12
        ]
        == PROMPT_VERSION
    )
    other = hashlib.sha256(
        (SYSTEM_PROMPT + " " + REWRITE_PROMPT).encode("utf-8")
    ).hexdigest()[:12]
    assert other != PROMPT_VERSION
