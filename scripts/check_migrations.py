"""Refuse a release whose database migration would break the version it replaces.

An image rollback (docs/RUNBOOKS.md, section 15) puts the previous code back on the new
schema, which is only safe if the migration was backwards compatible: it may add tables,
nullable columns and indexes, but must not remove or rename anything the old code reads or
writes, or add a required column the old code does not fill in. This script reads the
migration files (it never runs them) and reports each operation that would break that rule.

    python scripts/check_migrations.py                       # the newest migration only
    python scripts/check_migrations.py --since <git sha>     # migrations added since a commit
    python scripts/check_migrations.py --all                 # every migration (history)
    python scripts/check_migrations.py --allow ...           # report, but exit 0

A destructive step is sometimes right (a cleanup after every running version stopped using
a column). The deploy workflow then needs the "destructive migration" box ticked, which
is a deliberate decision rather than an accident, and the rollback runbook says to fix
forward instead.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "src" / "leyline" / "db" / "migrations" / "versions"

_ALWAYS = {
    "drop_table": "removes a table the previous version may read or write",
    "drop_column": "removes a column the previous version may read or write",
    "rename_table": "renames a table the previous version uses",
}
_SQL = re.compile(
    r"\b(DROP\s+(TABLE|COLUMN|TYPE|SCHEMA)|RENAME\s+(TO|COLUMN)|TRUNCATE)\b", re.I
)


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    why: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.why}"


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def inspect_source(source: str, name: str) -> list[Finding]:
    """Backward-incompatible operations in the ``upgrade()`` of one migration."""
    tree = ast.parse(source)
    upgrade = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
        ),
        None,
    )
    if upgrade is None:
        return [Finding(name, 1, "has no upgrade() function")]
    findings: list[Finding] = []
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method in _ALWAYS:
            findings.append(Finding(name, node.lineno, f"{method} {_ALWAYS[method]}"))
        elif method == "alter_column":
            if _keyword(node, "new_column_name") is not None:
                findings.append(
                    Finding(name, node.lineno, "alter_column renames a column")
                )
            if _is_false(_keyword(node, "nullable")):
                findings.append(
                    Finding(
                        name,
                        node.lineno,
                        "alter_column makes a column required (the previous version "
                        "may still write NULL)",
                    )
                )
            if _keyword(node, "type_") is not None:
                findings.append(
                    Finding(name, node.lineno, "alter_column changes a column's type")
                )
        elif method == "add_column":
            column = node.args[1] if len(node.args) > 1 else None
            if isinstance(column, ast.Call):
                required = _is_false(_keyword(column, "nullable"))
                has_default = (
                    _keyword(column, "server_default") is not None
                    or _keyword(column, "default") is not None
                )
                if required and not has_default:
                    findings.append(
                        Finding(
                            name,
                            node.lineno,
                            "add_column adds a required column without a default (the "
                            "previous version's inserts would fail)",
                        )
                    )
        elif method == "execute":
            text = " ".join(
                c.value
                for c in ast.walk(node)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            )
            if _SQL.search(text):
                findings.append(
                    Finding(
                        name, node.lineno, "raw SQL that drops, renames or truncates"
                    )
                )
    return findings


def migration_files() -> list[Path]:
    return sorted(p for p in VERSIONS.glob("0*.py"))


def added_since(sha: str) -> list[Path]:
    out = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "diff",
            "--name-only",
            "--diff-filter=A",
            f"{sha}..HEAD",
            "--",
            str(VERSIONS.relative_to(ROOT)),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(ROOT / line for line in out.splitlines() if line.endswith(".py"))


def select(args: argparse.Namespace) -> tuple[list[Path], str]:
    if args.all:
        return migration_files(), "every migration"
    if args.since:
        try:
            if not re.fullmatch(r"[0-9a-f]{7,40}", args.since):
                raise ValueError("not a commit id")
            return added_since(args.since), f"migrations added since {args.since}"
        except (ValueError, subprocess.CalledProcessError, FileNotFoundError):
            # The live image's tag is not a commit this repository knows (a workstation
            # deploy tags by timestamp), so what is new cannot be told: be careful instead.
            files = migration_files()
            return files[-1:], "the newest migration (the live commit is unknown)"
    files = migration_files()
    return files[-1:], "the newest migration"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_migrations.py")
    parser.add_argument(
        "--since", help="a git commit id: check migrations added after it"
    )
    parser.add_argument("--all", action="store_true", help="check every migration")
    parser.add_argument(
        "--allow", action="store_true", help="report problems but do not fail"
    )
    args = parser.parse_args(argv)

    files, what = select(args)
    findings: list[Finding] = []
    for path in files:
        findings.extend(inspect_source(path.read_text(encoding="utf-8"), path.name))
    print(f"checked {what}: {len(files)} file(s)")
    for finding in findings:
        print(f"  NOT BACKWARDS COMPATIBLE  {finding}")
    if findings and not args.allow:
        print(
            "\nAn image rollback would not be safe after this migration. If the change is "
            "deliberate (every running version has stopped using what it removes), run the "
            "deploy with the destructive-migration box ticked, and plan to fix forward "
            "instead of rolling back (docs/RUNBOOKS.md, section 15)."
        )
        return 1
    if findings:
        print("(allowed: a deliberate destructive migration)")
    elif files:
        print("backwards compatible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
