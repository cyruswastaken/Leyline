"""Engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

# Upper bound for any single statement, so a bad query cannot hold a
# connection (and its locks) indefinitely.
STATEMENT_TIMEOUT_MS = 30_000
CONNECT_TIMEOUT_SECONDS = 5

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _split_tls_option(url: str) -> tuple[str, dict[str, object]]:
    """Turn ``?ssl_context=true`` in the database URL into a real TLS setting.

    SQLAlchemy hands URL options to the driver as text, and pg8000 needs the
    boolean ``True`` (an ``ssl.SSLContext`` that verifies the server certificate
    against the system's trusted roots; Azure Database for PostgreSQL requires
    TLS). The option is removed from the URL and passed as a connect argument.
    """
    parsed = make_url(url)
    raw = parsed.query.get("ssl_context")
    if raw is None:
        return url, {}
    value = raw if isinstance(raw, str) else raw[-1]
    if value.lower() in _TRUE:
        tls: dict[str, object] = {"ssl_context": True}
    elif value.lower() in _FALSE:
        tls = {}
    else:
        raise ValueError("ssl_context in DATABASE_URL must be true or false")
    cleaned = parsed.difference_update_query(["ssl_context"])
    return cleaned.render_as_string(hide_password=False), tls


class Database:
    """Owns the engine. Connections are opened lazily, so constructing this
    does not require the database to be reachable."""

    def __init__(self, url: str, *, pool_size: int = 5) -> None:
        url, tls = _split_tls_option(url)
        self.engine: Engine = create_engine(
            url,
            pool_size=pool_size,
            max_overflow=pool_size,
            pool_timeout=10,
            pool_pre_ping=True,
            connect_args={"timeout": CONNECT_TIMEOUT_SECONDS, **tls},
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
