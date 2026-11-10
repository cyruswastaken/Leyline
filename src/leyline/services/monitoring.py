"""Reading security events back (for the auditor dashboard) and storing them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select

from leyline.db.database import Database
from leyline.db.models import SecurityEvent

DEFAULT_RECENT = 25
MAX_RECENT = 200

# Summary keys the dashboard already shows, mapped to event types.
_SUMMARY_KEYS = {
    "NORMAL_REQUEST": "normal_requests",
    "SECURITY_RULE_TRIGGERED": "security_rules_triggered",
    "RATE_LIMIT_EXCEEDED": "rate_limits_exceeded",
    "RETRIEVAL": "retrieval_requests",
    "NO_RESULTS_FOUND": "no_results_found",
    "MODEL_ERROR": "model_errors",
}


class DatabaseEventSink:
    """Appends events to ``security_events``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def __call__(self, event: dict[str, Any]) -> None:
        with self._db.session() as session:
            session.execute(
                insert(SecurityEvent).values(
                    ts=datetime.fromisoformat(event["timestamp"]),
                    event_type=event["event_type"],
                    user_id=event["user_id"],
                    status_code=event["status_code"],
                    reason=event["reason"],
                    details=event.get("details") or {},
                )
            )


def get_security_summary(database: Database) -> dict[str, Any]:
    """Counts by event type, with the dashboard's familiar keys plus ``by_type``."""
    with database.session() as session:
        rows = session.execute(
            select(SecurityEvent.event_type, func.count()).group_by(
                SecurityEvent.event_type
            )
        ).all()
    by_type = {event_type: int(count) for event_type, count in rows}
    summary: dict[str, Any] = {"total_events": sum(by_type.values())}
    for event_type, key in _SUMMARY_KEYS.items():
        summary[key] = by_type.get(event_type, 0)
    summary["by_type"] = dict(sorted(by_type.items()))
    return summary


def get_recent_events(
    database: Database,
    limit: int = DEFAULT_RECENT,
    *,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Newest first, in the shape the dashboard expects."""
    limit = max(1, min(limit, MAX_RECENT))
    query = select(SecurityEvent).order_by(SecurityEvent.id.desc()).limit(limit)
    if event_type:
        query = query.where(SecurityEvent.event_type == event_type)
    with database.session() as session:
        rows = session.scalars(query).all()
    events = []
    for row in rows:
        event: dict[str, Any] = {
            "timestamp": row.ts.isoformat(),
            "event_type": row.event_type,
            "user_id": row.user_id,
            "status_code": row.status_code,
            "reason": row.reason,
        }
        if row.details:
            event["details"] = row.details
        events.append(event)
    return events
