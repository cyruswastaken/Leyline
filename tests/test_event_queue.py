"""Security events stored off the request path."""

import threading
import time

import pytest
from sqlalchemy import text

from leyline.app import create_app
from leyline.services import logger as event_logger
from leyline.services.event_queue import QueuedEventSink
from leyline.services.logger import log_security_event, set_event_sink
from leyline.services.monitoring import get_recent_events
from tests.support import CSRF_HEADER, PERSONAS, FakeChatModel, login_as, make_settings


def event(n=0, kind="NORMAL_REQUEST"):
    return {
        "timestamp": "2026-11-24T00:00:00+00:00",
        "event_type": kind,
        "user_id": "u",
        "status_code": 200,
        "reason": f"r{n}",
    }


@pytest.fixture(autouse=True)
def no_sink_left_behind():
    yield
    set_event_sink(None)


def test_events_are_written_in_order_by_a_background_thread():
    stored = []
    sink = QueuedEventSink(stored.append, size=50)
    for n in range(20):
        sink(event(n))
    assert sink.flush()
    assert [e["reason"] for e in stored] == [f"r{n}" for n in range(20)]
    assert sink.written == 20 and sink.dropped == 0
    sink.close()


def test_a_slow_store_does_not_slow_the_caller():
    def slow(_):
        time.sleep(0.25)

    sink = QueuedEventSink(slow, size=10)
    started = time.perf_counter()
    for n in range(4):
        sink(event(n))
    assert time.perf_counter() - started < 0.1  # the caller never waited for a write
    sink.close(timeout=5)


def test_a_full_queue_drops_and_counts_new_events_and_says_so(caplog):
    release = threading.Event()
    stored = []

    def blocked(e):
        release.wait(5)
        stored.append(e)

    sink = QueuedEventSink(blocked, size=2)
    sink(event(0))  # taken by the writer, which then blocks
    time.sleep(0.1)
    for n in range(1, 6):
        sink(event(n))  # two fit in the queue, three are dropped
    assert sink.dropped == 3
    release.set()
    assert sink.flush()
    assert len(stored) == 3

    lines = [line for line in caplog.text.splitlines() if "queue_full" in line]
    assert len(lines) == 3 and all("EVENT_STORE_FAILED" in line for line in lines)
    sink.close()


def test_a_failing_store_is_counted_and_the_writer_carries_on(caplog):
    calls = []

    def flaky(e):
        calls.append(e["reason"])
        if e["reason"] == "r0":
            raise RuntimeError("store down")

    sink = QueuedEventSink(flaky, size=10)
    sink(event(0))
    sink(event(1))
    assert sink.flush()
    assert calls == ["r0", "r1"] and sink.failed == 1 and sink.written == 1
    assert "store_failed" in caplog.text
    sink.close()


def test_closing_writes_what_is_queued_and_refuses_later_events(caplog):
    stored = []
    sink = QueuedEventSink(lambda e: (time.sleep(0.05), stored.append(e)), size=10)
    for n in range(3):
        sink(event(n))
    sink.close()
    assert len(stored) == 3
    sink(event(9))
    assert len(stored) == 3 and "sink_closed" in caplog.text
    sink.close()  # twice is fine


def test_the_queue_needs_a_slot():
    with pytest.raises(ValueError):
        QueuedEventSink(lambda e: None, size=0)


def test_replacing_the_sink_flushes_and_stops_the_old_one():
    stored = []
    old = QueuedEventSink(stored.append, size=10)
    set_event_sink(old)
    log_security_event("X", "u", 200, "r")
    set_event_sink(None)  # flushes the old sink
    assert [e["event_type"] for e in stored] == ["X"]


def test_log_security_event_hands_events_to_the_queued_sink():
    stored = []
    sink = QueuedEventSink(stored.append, size=10)
    set_event_sink(sink)
    log_security_event("A", "u", 200, "r")
    assert sink.flush() and stored and stored[0]["event_type"] == "A"
    assert event_logger._sink is sink
    sink.close()


# --- through the real app and database ---------------------------------------------------


def test_the_app_stores_events_through_the_queue_when_configured(
    db, redis, blobs, embedder, add_doc
):
    add_doc(
        "Password reset",
        "# Passwords\n\nReset your password in the self-service portal.",
        ["*"],
    )
    app = create_app(
        make_settings(event_queue_size=100),
        redis_client=redis,
        database=db,
        blob_store=blobs,
        embedder=embedder,
        chat_model=FakeChatModel(),
    )
    app.config.update(TESTING=True)
    client = app.test_client()
    token = login_as(client, PERSONAS["employee"])
    with db.session() as session:
        before = session.scalar(text("select count(*) from security_events"))

    client.post(
        "/chat",
        json={"message": "How do I reset my password?"},
        headers={CSRF_HEADER: token},
    )

    sink = event_logger._sink
    assert isinstance(sink, QueuedEventSink) and sink.flush()
    types = {e["event_type"] for e in get_recent_events(db, 20)}
    assert {"RETRIEVAL", "NORMAL_REQUEST"} <= types
    with db.session() as session:
        assert session.scalar(text("select count(*) from security_events")) > before


def test_the_default_is_the_synchronous_sink(db, redis, blobs, embedder):
    from leyline.services.monitoring import DatabaseEventSink

    create_app(
        make_settings(),
        redis_client=redis,
        database=db,
        blob_store=blobs,
        embedder=embedder,
        chat_model=FakeChatModel(),
    )
    assert isinstance(event_logger._sink, DatabaseEventSink)
