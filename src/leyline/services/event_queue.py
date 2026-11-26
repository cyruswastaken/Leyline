"""Store security events off the request path.

Storing an event is a database write; doing it inside the request adds its latency
to every answer and ties the request to the event store's health. ``QueuedEventSink``
accepts an event in microseconds and a background thread writes it. The queue is
bounded: when the writer cannot keep up, new events are dropped and counted rather
than letting memory grow or making requests wait, and the stdout line that every
event also gets (see ``services/logger.py``) remains the record.

Like the synchronous sink it never raises into a request. Pending events are flushed
when the process exits normally; a hard kill can lose what was still queued, which
is why stdout stays the first copy.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
from datetime import UTC, datetime
from typing import Any

from leyline.services.logger import EventSink

logger = logging.getLogger("secure_helpdesk")

_STOP: dict[str, Any] = {}


def _report(reason: str, lost: str) -> None:
    logger.error(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event_type": "EVENT_STORE_FAILED",
                "lost_event_type": lost,
                "reason": reason,
            }
        )
    )


class QueuedEventSink:
    def __init__(self, inner: EventSink, *, size: int = 1000) -> None:
        if size < 1:
            raise ValueError("the queue needs at least one slot")
        self._inner = inner
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=size)
        self._lock = threading.Lock()
        self.dropped = 0
        self.failed = 0
        self.written = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="event-writer", daemon=True
        )
        self._thread.start()
        atexit.register(self.close)

    def __call__(self, event: dict[str, Any]) -> None:
        if self._closed:
            _report("sink_closed", str(event.get("event_type")))
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self.dropped += 1
            _report("queue_full", str(event.get("event_type")))

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is _STOP:
                    return
                try:
                    self._inner(event)
                    self.written += 1
                except Exception:
                    self.failed += 1
                    _report("store_failed", str(event.get("event_type")))
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued so far is written (or the time is up)."""
        done = threading.Event()

        def wait() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=wait, daemon=True).start()
        return done.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        """Write what is queued, then stop the writer. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout)
