"""Alembic environment.

Migrations always run on a connection supplied by ``leyline.db.migrate``, so
no database URL (and no password) is ever stored in an ini file.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection

from leyline.db.models import Base

config = context.config
target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported; use leyline.db.migrate")

connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("no connection supplied; use leyline.db.migrate.upgrade()")
_run(connection)
