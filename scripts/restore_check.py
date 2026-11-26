"""Check a restored copy of the Leyline database.

Used by infra/restore-drill.ps1 against a temporary server restored from a backup. It reads
connection details from the environment (never the command line) and prints a JSON summary
that the script turns into pass or fail:

    PGHOST      the restored server's address
    PGUSER      the Entra user to sign in as
    PGPASSWORD  a short-lived Entra access token for that user (the password)

It only reads. Nothing here writes to the database.
"""

from __future__ import annotations

import json
import os
import sys

import pg8000.dbapi


def check(connection: pg8000.dbapi.Connection) -> dict[str, object]:
    cursor = connection.cursor()

    def one(sql: str) -> object:
        cursor.execute(sql)
        row = cursor.fetchone()
        return row[0] if row else None

    summary: dict[str, object] = {
        "schema_revision": one("SELECT version_num FROM alembic_version"),
        "documents": one("SELECT count(*) FROM documents WHERE deleted_at IS NULL"),
        "chunks": one("SELECT count(*) FROM chunks"),
        "chunks_with_vectors": one(
            "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"
        ),
        # A live document with no access groups would be unreadable by everyone; zero is right.
        "live_documents_without_acl": one(
            "SELECT count(*) FROM documents d WHERE d.deleted_at IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM document_acl a WHERE a.document_id = d.id)"
        ),
        "conversations": one("SELECT count(*) FROM conversations"),
        "tickets": one("SELECT count(*) FROM tickets"),
        "security_events": one("SELECT count(*) FROM security_events"),
        "newest_security_event": str(one("SELECT max(ts) FROM security_events")),
        "append_only_trigger": one(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'security_events_append_only'"
        ),
        "vector_extension": one(
            "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
        ),
    }
    cursor.close()
    return summary


def main() -> int:
    try:
        connection = pg8000.dbapi.connect(
            host=os.environ["PGHOST"],
            user=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"],
            database=os.environ.get("PGDATABASE", "leyline"),
            ssl_context=True,
            timeout=30,
        )
    except KeyError as exc:
        print(f"missing environment variable {exc}", file=sys.stderr)
        return 2
    try:
        print(json.dumps(check(connection)))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
