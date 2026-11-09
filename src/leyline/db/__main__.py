"""``python -m leyline.db upgrade|current|check``"""

from __future__ import annotations

import argparse
import sys

from leyline.config import get_settings
from leyline.db import migrate
from leyline.db.database import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leyline.db")
    parser.add_argument("command", choices=["upgrade", "current", "check", "revision"])
    parser.add_argument("-m", "--message", help="description for a new revision")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.database_url is None:
        print("DATABASE_URL is not configured", file=sys.stderr)
        return 2

    database = Database(settings.database_url.get_secret_value(), pool_size=1)
    try:
        if args.command == "upgrade":
            migrate.upgrade(database.engine)
            print(f"database is at {migrate.current_revision(database.engine)}")
        elif args.command == "revision":
            if not args.message:
                print("revision needs -m MESSAGE", file=sys.stderr)
                return 2
            if not migrate.is_current(database.engine):
                print("migrate the database first: upgrade", file=sys.stderr)
                return 1
            print(f"created {migrate.create_revision(database.engine, args.message)}")
        elif args.command == "current":
            print(migrate.current_revision(database.engine) or "(none)")
        else:
            if migrate.is_current(database.engine):
                print("schema is current")
            else:
                print(
                    f"schema is {migrate.current_revision(database.engine)}, "
                    f"expected {migrate.head_revision()}",
                    file=sys.stderr,
                )
                return 1
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
