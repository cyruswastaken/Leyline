import ast
from typing import Any

from leyline.services.logger import LOG_FILE


def get_security_events() -> list[dict[str, Any]]:
    """
    Read and return all valid security events.
    """

    events: list[dict[str, Any]] = []

    if not LOG_FILE.exists():
        return events

    with open(LOG_FILE, encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line:
                continue

            try:
                event = ast.literal_eval(line)
                events.append(event)

            except (ValueError, SyntaxError):
                continue

    return events


def get_security_summary() -> dict[str, Any]:
    """
    Return a summary of all security events.
    """

    events = get_security_events()

    summary = {
        "total_events": len(events),
        "normal_requests": 0,
        "security_rules_triggered": 0,
        "rate_limits_exceeded": 0,
        "retrieval_requests": 0,
        "no_results_found": 0,
        "model_errors": 0,
    }

    for event in events:
        event_type = event.get("event_type")

        if event_type == "NORMAL_REQUEST":
            summary["normal_requests"] += 1

        elif event_type == "SECURITY_RULE_TRIGGERED":
            summary["security_rules_triggered"] += 1

        elif event_type == "RATE_LIMIT_EXCEEDED":
            summary["rate_limits_exceeded"] += 1

        elif event_type == "RETRIEVAL":
            summary["retrieval_requests"] += 1

        elif event_type == "NO_RESULTS_FOUND":
            summary["no_results_found"] += 1

        elif event_type == "MODEL_ERROR":
            summary["model_errors"] += 1

    return summary


def get_recent_events(limit: int = 10) -> list[dict[str, Any]]:
    """
    Return the most recent security events.
    """

    events = get_security_events()

    return events[-limit:][::-1]
