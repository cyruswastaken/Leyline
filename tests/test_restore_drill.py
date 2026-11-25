"""The restore drill: its checks work on a real database, and the script is safe by construction."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import restore_check  # noqa: E402

SCRIPT = (ROOT / "infra" / "restore-drill.ps1").read_text("utf-8")


def test_the_checks_describe_a_healthy_database(db, add_doc):
    add_doc("Password reset", "# P\n\nReset your password in the portal.", ["*"])
    with db.engine.connect() as connection:
        summary = restore_check.check(connection.connection.driver_connection)

    assert summary["schema_revision"]  # the migration the code expects
    assert summary["documents"] >= 1 and summary["chunks"] >= 1
    assert summary["live_documents_without_acl"] == 0
    assert summary["append_only_trigger"] == 1
    assert summary["vector_extension"] == 1


def test_a_live_document_without_an_access_list_is_reported(db, add_doc):
    """The check that catches a restore that lost ACL rows: such a document is invisible to everyone."""
    from sqlalchemy import text

    add_doc("Orphan", "# O\n\nbody", ["*"])
    with db.session() as session:
        session.execute(text("DELETE FROM document_acl"))
    with db.engine.connect() as connection:
        summary = restore_check.check(connection.connection.driver_connection)
    assert summary["live_documents_without_acl"] >= 1


def test_the_check_reads_only(db):
    source = (ROOT / "scripts" / "restore_check.py").read_text("utf-8")
    assert not re.search(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE)\b",
        source.split('"""', 2)[2],
    )


def test_the_script_only_ever_deletes_a_server_it_created():
    assert "leyline-pg-drill-" in SCRIPT
    assert re.search(r"-notmatch '\^leyline-pg-drill-\[a-z0-9\]\{6\}\$'", SCRIPT)
    deletes = re.findall(r"flexible-server delete[^\n]*", SCRIPT)
    assert deletes and all("$drill" in d for d in deletes)
    assert "$created" in SCRIPT  # nothing is deleted unless the restore happened
    assert "finally" in SCRIPT


def test_the_script_never_changes_the_live_server_and_previews_first():
    assert "SupportsShouldProcess" in SCRIPT and "ShouldProcess" in SCRIPT
    for action in ("update", "ad-admin create", "firewall-rule create"):
        for line in re.findall(rf"flexible-server {action}[^\n]*", SCRIPT):
            assert "$drill" in line and "$source" not in line, line
    assert (
        "--source-server $source" in SCRIPT
    )  # the only use of the live server is as the source


def test_the_token_goes_in_the_environment_never_on_a_command_line():
    assert "$env:PGPASSWORD = $token" in SCRIPT
    assert "Remove-Item Env:PGHOST, Env:PGUSER, Env:PGPASSWORD" in SCRIPT
    assert not re.search(r"restore_check\.py[^\n]*\$token", SCRIPT)
    assert "Write-Host $token" not in SCRIPT


def test_the_script_reports_recovery_time_and_point():
    assert "Recovery time" in SCRIPT and "Recovery point" in SCRIPT
    assert "PASSED" in SCRIPT and "FAILED" in SCRIPT
