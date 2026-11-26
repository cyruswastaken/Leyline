"""Release safety: the migration check and the post-deploy smoke test."""

import sys
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_migrations  # noqa: E402
import smoke_test  # noqa: E402

# --- the migration check ------------------------------------------------------------------


def migration(body: str) -> str:
    return f"def upgrade():\n{body}\n\ndef downgrade():\n    pass\n"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("    op.drop_table('x')", "drop_table"),
        ("    op.drop_column('t', 'c')", "drop_column"),
        ("    op.rename_table('a', 'b')", "rename_table"),
        ("    op.alter_column('t', 'c', new_column_name='d')", "renames a column"),
        ("    op.alter_column('t', 'c', nullable=False)", "makes a column required"),
        ("    op.alter_column('t', 'c', type_=sa.Text())", "changes a column's type"),
        (
            "    op.add_column('t', sa.Column('c', sa.Text(), nullable=False))",
            "required column without a default",
        ),
        ("    op.execute('ALTER TABLE t DROP COLUMN c')", "raw SQL"),
        ("    op.execute('ALTER TABLE t RENAME TO u')", "raw SQL"),
        ("    op.execute('TRUNCATE t')", "raw SQL"),
    ],
)
def test_a_destructive_step_is_reported(body, fragment):
    findings = check_migrations.inspect_source(migration(body), "0099_x.py")
    assert findings and fragment in str(findings[0])


@pytest.mark.parametrize(
    "body",
    [
        "    op.create_table('t', sa.Column('id', sa.Integer()))",
        "    op.add_column('t', sa.Column('c', sa.Text(), nullable=True))",
        "    op.add_column('t', sa.Column('c', sa.Text(), nullable=False, server_default='x'))",
        "    op.create_index('ix', 't', ['c'])",
        "    op.drop_index('ix', table_name='t')",
        "    op.execute('CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql')",
        "    op.alter_column('t', 'c', nullable=True)",
    ],
)
def test_an_additive_step_passes(body):
    assert check_migrations.inspect_source(migration(body), "0099_x.py") == []


def test_only_upgrade_counts_not_downgrade():
    source = "def upgrade():\n    op.create_table('t')\n\ndef downgrade():\n    op.drop_table('t')\n"
    assert check_migrations.inspect_source(source, "0099_x.py") == []


def test_a_migration_with_no_upgrade_is_flagged():
    assert check_migrations.inspect_source("x = 1", "0099_x.py")


def test_the_shipped_migrations_are_all_additive_or_listed():
    """The history itself: any destructive step must be a known, decided one."""
    problems = {
        finding.file: finding.why
        for path in check_migrations.migration_files()
        for finding in check_migrations.inspect_source(
            path.read_text(encoding="utf-8"), path.name
        )
    }
    # 0002 changes the embedding column's dimension: the first model was never in production.
    assert set(problems) <= {"0002_vector_dimension_and_index.py"}, problems


def test_the_command_fails_on_a_destructive_migration_unless_allowed(
    tmp_path, monkeypatch, capsys
):
    bad = tmp_path / "0100_bad.py"
    bad.write_text(migration("    op.drop_column('t', 'c')"), encoding="utf-8")
    monkeypatch.setattr(check_migrations, "migration_files", lambda: [bad])

    assert check_migrations.main([]) == 1
    out = capsys.readouterr().out
    assert "NOT BACKWARDS COMPATIBLE" in out and "fix forward" in out
    assert check_migrations.main(["--allow"]) == 0
    assert "allowed" in capsys.readouterr().out


def test_an_unknown_live_commit_falls_back_to_the_newest_migration(monkeypatch, capsys):
    files = check_migrations.migration_files()
    assert check_migrations.main(["--since", "not-a-sha"]) in (0, 1)
    assert "live commit is unknown" in capsys.readouterr().out
    selected, _ = check_migrations.select(
        check_migrations.argparse.Namespace(all=False, since="deadbeef0000")
    )
    assert selected == files[-1:]


def test_the_newest_migration_is_what_runs_by_default(capsys):
    check_migrations.main([])
    assert check_migrations.migration_files()[-1].name in " ".join(
        p.name for p in check_migrations.migration_files()
    )
    assert "checked the newest migration" in capsys.readouterr().out


# --- the smoke test, against a real running app -------------------------------------------


@pytest.fixture
def running_site(db_app):
    server = make_server("127.0.0.1", 0, db_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_a_healthy_closed_site_passes_every_check(running_site):
    results = smoke_test.run_checks(running_site)
    failed = [r for r in results if not r.ok]
    assert not failed, failed
    assert len(results) >= 10


def test_the_command_reports_and_exits_zero_for_a_healthy_site(running_site, capsys):
    assert smoke_test.main([running_site]) == 0
    assert "checks passed" in capsys.readouterr().out


def test_a_site_that_is_down_fails(capsys):
    assert smoke_test.main(["http://127.0.0.1:9"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_a_page_that_a_stranger_can_open_fails_the_check(running_site, monkeypatch):
    real_get = smoke_test.get

    def open_door(url):
        if url.endswith("/monitoring/summary"):
            return smoke_test.Reply(200, {}, "{}")
        return real_get(url)

    monkeypatch.setattr(smoke_test, "get", open_door)
    failed = [r for r in smoke_test.run_checks(running_site) if not r.ok]
    assert [r.name for r in failed] == [
        "/monitoring/summary refuses a signed-out visitor"
    ]
    assert "HTTP 200" in failed[0].detail


def test_a_weak_content_security_policy_fails_the_check(running_site, monkeypatch):
    real_get = smoke_test.get

    def weak(url):
        reply = real_get(url)
        if url.endswith("/welcome"):
            return smoke_test.Reply(
                reply.status,
                {
                    **reply.headers,
                    "content-security-policy": "script-src 'unsafe-inline'",
                },
                reply.body,
            )
        return reply

    monkeypatch.setattr(smoke_test, "get", weak)
    failed = [r.name for r in smoke_test.run_checks(running_site) if not r.ok]
    assert failed == ["the Content-Security-Policy has no unsafe-inline"]


def test_the_usage_message_for_a_missing_address(capsys):
    assert smoke_test.main([]) == 2
    assert smoke_test.main(["ftp://x"]) == 2
