"""The application's database role can add events but never change or remove them.

Applies deploy/sql/app_role.sql to a throwaway role and tries every operation the
role must not be able to do. The triggers from migration 0005 already refuse UPDATE
and DELETE; this also covers TRUNCATE, which triggers do not see.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "sql" / "app_role.sql"
TABLES = ("security_events", "audit_events")


@pytest.fixture
def app_role(db):
    name = "probe_" + uuid.uuid4().hex[:8]
    with db.session() as session:
        session.execute(text(f"CREATE ROLE {name} NOLOGIN"))
        session.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {name}"))
        for statement in (
            SCRIPT.read_text(encoding="utf-8").format(role=name).split(";")
        ):
            body = "\n".join(
                line for line in statement.splitlines() if not line.startswith("--")
            ).strip()
            if body:
                session.execute(text(body))
    yield name
    with db.session() as session:
        session.execute(text(f"DROP OWNED BY {name}"))
        session.execute(text(f"DROP ROLE {name}"))


def as_role(db, role, sql, **params):
    """Run one statement as the role; return the exception or None."""
    with db.session() as session:
        session.execute(text(f"SET LOCAL ROLE {role}"))
        try:
            session.execute(text(sql), params)
        except DBAPIError as exc:
            session.rollback()  # the failed transaction cannot be committed
            return exc
        return None


def test_the_role_can_add_and_read_events(db, app_role):
    assert (
        as_role(
            db,
            app_role,
            "INSERT INTO security_events (event_type, user_id, status_code, reason) "
            "VALUES ('PROBE', 'u', 200, 'allowed path')",
        )
        is None
    )
    assert as_role(db, app_role, "SELECT count(*) FROM security_events") is None


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize(
    "statement",
    ["UPDATE {t} SET id = id", "DELETE FROM {t}", "TRUNCATE {t}"],
)
def test_the_role_cannot_change_or_remove_events(db, app_role, table, statement):
    error = as_role(db, app_role, statement.format(t=table))
    assert error is not None and "permission denied" in str(error).lower()


def test_the_role_cannot_drop_the_append_only_trigger(db, app_role):
    error = as_role(
        db, app_role, "DROP TRIGGER security_events_append_only ON security_events"
    )
    assert error is not None  # not the owner, so it cannot


def test_without_the_script_truncate_would_succeed(db):
    """Why the script exists: row triggers do not stop TRUNCATE."""
    name = "probe_" + uuid.uuid4().hex[:8]
    with db.session() as session:
        session.execute(text(f"CREATE ROLE {name} NOLOGIN"))
        session.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {name}"))
    try:
        assert as_role(db, name, "TRUNCATE security_events") is None
    finally:
        with db.session() as session:
            session.execute(text(f"DROP OWNED BY {name}"))
            session.execute(text(f"DROP ROLE {name}"))
