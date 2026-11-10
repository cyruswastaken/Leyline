"""Structured security and system events.

``log_security_event`` writes one JSON line to stdout (what container platforms
collect) and hands the same event to the registered sink, which stores it in the
append-only ``security_events`` table. A sink that fails never breaks the
request: the failure is logged and the stdout line remains. Events carry ids,
reasons and counts, never prompts, answers or document text.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

EventSink = Callable[[dict[str, Any]], None]

logger = logging.getLogger("secure_helpdesk")
logger.setLevel(logging.INFO)
logger.propagate = False

# Prevent duplicate handlers when the module is re-imported.
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

_sink: EventSink | None = None


def set_event_sink(sink: EventSink | None) -> None:
    """Where events are stored besides stdout (None disables storage)."""
    global _sink
    _sink = sink


def log_security_event(
    event_type: str,
    user_id: str,
    status_code: int,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    event: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        "user_id": user_id,
        "status_code": status_code,
        "reason": reason,
    }
    if details:
        event["details"] = details

    logger.info(json.dumps(event, default=str))

    sink = _sink
    if sink is not None:
        try:
            sink(event)
        except Exception:
            logger.error(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "event_type": "EVENT_STORE_FAILED",
                        "lost_event_type": event_type,
                    }
                )
            )
