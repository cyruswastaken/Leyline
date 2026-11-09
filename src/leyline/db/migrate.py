"""Programmatic Alembic helpers (no ini file, no URL stored on disk)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Serializes concurrent migrators (e.g. two replicas starting together).
_ADVISORY_LOCK_KEY = 727_274


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    config = _config()
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}
        )
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade(engine: Engine, revision: str) -> None:
    config = _config()
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}
        )
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def create_revision(engine: Engine, message: str) -> Path:
    """Autogenerate a new migration by comparing the models with the database.

    Run against a database that is at the current head. Review the generated
    file before committing it.
    """
    config = _config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        script = command.revision(config, message=message, autogenerate=True)
    if script is None or isinstance(script, list):
        raise RuntimeError("no revision was created")
    return Path(script.path)


def head_revision() -> str:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    if head is None:
        raise RuntimeError("no migrations found")
    return head


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_current(engine: Engine) -> bool:
    """True when the database schema is exactly at the code's head revision."""
    return current_revision(engine) == head_revision()
