"""Durable, append-only security events and the auditor's SQL-backed view."""

import json
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from leyline.app import create_app
from leyline.services import logger as event_logger
from leyline.services.logger import log_security_event, set_event_sink
from leyline.services.monitoring import (
    DatabaseEventSink,
    get_recent_events,
    get_security_summary,
)
from tests.support import CSRF_HEADER, PERSONAS, FakeChatModel, login_as, make_settings


@pytest.fixture
def stored(db):
    """Events logged inside the test are written to the real table."""
    set_event_sink(DatabaseEventSink(db))
    yield db
    set_event_sink(None)


def rows(db):
    with db.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT event_type, user_id, status_code, reason, details FROM security_events ORDER BY id"
            )
        ).all()


# --- storage ------------------------------------------------------------------------------


def test_an_event_is_stored_with_its_fields(stored):
    log_security_event(
        "RETRIEVAL", "u1", 200, "retrieval_completed", {"returned_count": 3}
    )
    log_security_event("RATE_LIMIT_EXCEEDED", "u2", 429, "rate_limit_exceeded")
    first, second = rows(stored)
    assert first.event_type == "RETRIEVAL" and first.user_id == "u1"
    assert first.details == {"returned_count": 3}
    assert second.status_code == 429 and second.details == {}


def test_events_are_also_written_to_stdout_as_json(stored):
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = Capture()
    event_logger.logger.addHandler(handler)
    try:
        log_security_event("NORMAL_REQUEST", "u1", 200, "request_allowed")
    finally:
        event_logger.logger.removeHandler(handler)
    event = json.loads(captured[0])
    assert event["event_type"] == "NORMAL_REQUEST" and "timestamp" in event


def test_a_failing_store_never_breaks_the_caller_and_is_reported():
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    def broken(_event):
        raise RuntimeError("database is down")

    handler = Capture()
    event_logger.logger.addHandler(handler)
    set_event_sink(broken)
    try:
        log_security_event(
            "RETRIEVAL", "u1", 200, "retrieval_completed"
        )  # must not raise
    finally:
        set_event_sink(None)
        event_logger.logger.removeHandler(handler)
    assert any(
        "EVENT_STORE_FAILED" in line and "RETRIEVAL" in line for line in captured
    )
    assert not any("database is down" in line for line in captured)


# --- append-only ----------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["security_events", "audit_events"])
def test_events_cannot_be_updated_or_deleted(db, table):
    with db.engine.begin() as connection:
        if table == "security_events":
            connection.execute(
                text(
                    "INSERT INTO security_events (event_type, user_id, status_code, reason) VALUES ('X', 'u', 200, 'r')"
                )
            )
        else:
            connection.execute(
                text(
                    "INSERT INTO audit_events (actor_id, action, target_type) VALUES ('a', 'x', 'document')"
                )
            )
    column = "reason" if table == "security_events" else "action"
    for statement in (
        f"UPDATE {table} SET {column} = 'tampered'",
        f"DELETE FROM {table}",
    ):
        with (
            pytest.raises(DBAPIError, match="append-only"),
            db.engine.begin() as connection,
        ):
            connection.execute(text(statement))
    with db.engine.connect() as connection:
        assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 1


# --- through the app ---------------------------------------------------------------------------


@pytest.fixture
def client(db, redis, blobs, embedder):
    app = create_app(
        make_settings(),
        redis_client=redis,
        database=db,
        blob_store=blobs,
        embedder=embedder,
        chat_model=FakeChatModel(),
    )
    app.config.update(TESTING=True)
    yield app.test_client()
    set_event_sink(None)


def ask(client, persona, message):
    token = login_as(client, PERSONAS[persona])
    return client.post("/chat", json={"message": message}, headers={CSRF_HEADER: token})


def test_chat_activity_is_stored_without_the_question_or_the_answer(
    client, add_doc, db
):
    add_doc(
        "Password reset", "# Passwords\n\nReset your password in the portal.", ["*"]
    )
    ask(client, "employee", "how do I reset my password zebrafish?")
    stored_events = rows(db)
    kinds = [r.event_type for r in stored_events]
    assert "RETRIEVAL" in kinds and "NORMAL_REQUEST" in kinds
    everything = json.dumps([[str(c) for c in r] for r in stored_events])
    assert "zebrafish" not in everything and "self-service" not in everything
    assert {r.user_id for r in stored_events} == {PERSONAS["employee"].user_id}


def test_a_hermetic_app_with_no_database_stores_nothing(redis):
    from tests.support import StubDatabase

    create_app(make_settings(), redis_client=redis, database=StubDatabase())
    assert event_logger._sink is None


def test_the_auditor_sees_counts_and_recent_events_newest_first(client, add_doc, db):
    add_doc(
        "Password reset", "# Passwords\n\nReset your password in the portal.", ["*"]
    )
    ask(client, "employee", "reset my password")
    ask(client, "employee", "reset my password again")
    login_as(client, PERSONAS["auditor"])

    summary = client.get("/monitoring/summary").get_json()
    assert summary["retrieval_requests"] == 2
    assert summary["normal_requests"] == 2 and summary["no_results_found"] == 0
    assert summary["by_type"]["RETRIEVAL"] == 2
    assert summary["total_events"] == sum(summary["by_type"].values())

    events = client.get("/monitoring/events?limit=3").get_json()
    assert len(events) == 3
    stamps = [e["timestamp"] for e in events]
    assert stamps == sorted(stamps, reverse=True)
    only = client.get("/monitoring/events?type=NORMAL_REQUEST").get_json()
    assert [e["event_type"] for e in only] == ["NORMAL_REQUEST"] * 2


def test_the_event_limit_is_capped(db):
    set_event_sink(DatabaseEventSink(db))
    try:
        for i in range(5):
            log_security_event("X", "u", 200, f"r{i}")
    finally:
        set_event_sink(None)
    assert len(get_recent_events(db, 100000)) == 5
    assert len(get_recent_events(db, 0)) == 1  # at least one
    assert get_security_summary(db)["total_events"] == 5
