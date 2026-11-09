"""Engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

# Upper bound for any single statement, so a bad query cannot hold a
# connection (and its locks) indefinitely.
STATEMENT_TIMEOUT_MS = 30_000
CONNECT_TIMEOUT_SECONDS = 5


class Database:
    """Owns the engine. Connections are opened lazily, so constructing this
    does not require the database to be reachable."""

    def __init__(self, url: str, *, pool_size: int = 5) -> None:
        self.engine: Engine = create_engine(
            url,
            pool_size=pool_size,
            max_overflow=pool_size,
            pool_timeout=10,
            pool_pre_ping=True,
            connect_args={"timeout": CONNECT_TIMEOUT_SECONDS},
        )

        @event.listens_for(self.engine, "connect")
        def _set_timeouts(dbapi_connection: object, _: ConnectionPoolEntry) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            finally:
                cursor.close()
            dbapi_connection.commit()  # type: ignore[attr-defined]

        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A transaction: committed on success, rolled back on any error."""
        with self._sessions() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def ping(self) -> bool:
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar() == 1

    def connection(self) -> Connection:
        return self.engine.connect()

    def dispose(self) -> None:
        self.engine.dispose()
