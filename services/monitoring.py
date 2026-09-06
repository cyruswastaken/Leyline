import ast
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = BASE_DIR / "logs" / "security_events.log"


def get_security_events() -> list:
    """
    Read and return all valid security events.
    """

    events = []

    if not LOG_FILE.exists():
        return events

    with open(LOG_FILE, "r", encoding="utf-8") as file:
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


def get_security_summary() -> dict:
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
        "no_results_found": 0
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

    return summary


def get_recent_events(limit: int = 10) -> list:
    """
    Return the most recent security events.
    """

    events = get_security_events()

    return events[-limit:][::-1]