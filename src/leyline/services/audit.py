"""Audit trail for administrative actions.

Written in the same transaction as the change it describes, so an action and
its audit record either both happen or neither does.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from leyline.db.models import AuditEvent


def record(
    session: Session,
    *,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
        )
    )
