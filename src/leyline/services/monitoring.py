"""Reading security events back (for the auditor dashboard) and storing them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, Numeric, func, insert, literal_column, select

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
    summary["usage"] = get_usage_summary(database)
    return summary


USAGE_DAYS = 7


def get_usage_summary(database: Database, days: int = USAGE_DAYS) -> dict[str, Any]:
    """Tokens, cost and the no-answer rate over the last ``days`` days, from the
    numbers stored on each answered turn's event (never from conversation text).

    ``no_answer_rate`` is retrievals that found nothing readable, as a share of all
    retrievals, so it moves when the corpus or the relevance floors drift."""
    since = datetime.now(UTC) - timedelta(days=days)
    # A literal, not a bound parameter: the driver would send the select list and the
    # GROUP BY as different placeholders and Postgres could not match them.
    day = func.date_trunc(literal_column("'day'"), SecurityEvent.ts).label("day")
    prompt = SecurityEvent.details["prompt_tokens"].astext.cast(BigInteger)
    completion = SecurityEvent.details["completion_tokens"].astext.cast(BigInteger)
    cost = SecurityEvent.details["cost_usd"].astext.cast(Numeric)
    with database.session() as session:
        rows = session.execute(
            select(
                day,
                func.count(),
                func.coalesce(func.sum(prompt), 0),
                func.coalesce(func.sum(completion), 0),
                func.coalesce(func.sum(cost), 0),
            )
            .where(
                SecurityEvent.event_type == "NORMAL_REQUEST",
                SecurityEvent.ts >= since,
                SecurityEvent.details.has_key("prompt_tokens"),
            )
            .group_by(day)
            .order_by(day)
        ).all()
        counts: dict[str, int] = {
            event_type: int(count)
            for event_type, count in session.execute(
                select(SecurityEvent.event_type, func.count())
                .where(
                    SecurityEvent.event_type.in_(("RETRIEVAL", "NO_RESULTS_FOUND")),
                    SecurityEvent.ts >= since,
                )
                .group_by(SecurityEvent.event_type)
            ).all()
        }
    retrievals = int(counts.get("RETRIEVAL", 0))
    return {
        "days": days,
        "by_day": [
            {
                "day": row[0].date().isoformat(),
                "answers": int(row[1]),
                "prompt_tokens": int(row[2]),
                "completion_tokens": int(row[3]),
                "cost_usd": round(float(row[4]), 6),
            }
            for row in rows
        ],
        "no_answer_rate": (
            round(int(counts.get("NO_RESULTS_FOUND", 0)) / retrievals, 4)
            if retrievals
            else 0.0
        ),
    }


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
