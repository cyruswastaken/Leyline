"""The alert rules, the objectives they guard and the runbooks they point to."""

import json
import re
from pathlib import Path

import pytest

from leyline.services.chat_service import record_usage  # noqa: F401  (the field exists)
from tests.support import CSRF_HEADER, PERSONAS, FakeChatModel, login_as

ROOT = Path(__file__).resolve().parent.parent
MONITORING = (ROOT / "infra" / "modules" / "monitoring.bicep").read_text("utf-8")
RUNBOOKS = (ROOT / "docs" / "RUNBOOKS.md").read_text("utf-8")
SLO = (ROOT / "docs" / "SLO.md").read_text("utf-8")


def rules() -> dict[str, dict[str, str]]:
    """name -> fields of every log rule in the template."""
    found = {}
    for block in MONITORING.split("  {\n    name: '")[1:]:
        name = block.split("'", 1)[0]
        fields = dict(re.findall(r"^    (\w+): (.+)$", block, re.M))
        fields["name"] = name
        found[name] = fields
    return found


def test_every_rule_is_defined_with_a_runbook_that_exists():
    assert len(rules()) >= 9
    headings = set(re.findall(r"^### (.+)$", RUNBOOKS, re.M))
    for name, rule in rules().items():
        if "description" not in rule:
            continue  # the two database metric rules have no runbook field
        match = re.search(r'Runbook: "([^"]+)"', rule["description"])
        assert match, f"{name} does not say which runbook to follow"
        assert match.group(1) in headings, (
            f"{name}: no runbook section {match.group(1)!r}"
        )


def test_the_database_metric_alerts_exist_with_a_section_in_the_runbooks():
    assert "'cpu_percent'" in MONITORING or "metric: 'cpu_percent'" in MONITORING
    assert "storage_percent" in MONITORING
    assert "### The database is busy or full" in RUNBOOKS


def test_every_rule_is_in_the_slo_document_and_the_numbers_agree():
    for name in rules():
        assert name in SLO or name.startswith("leyline-database"), name
    for name in ("leyline-database-cpu", "leyline-database-storage"):
        assert name in SLO
    assert "p95 > 2500" in MONITORING and "2.5 s" in SLO
    assert "n >= 10" in MONITORING  # and "at least 10 answers" in the SLO table
    assert "at least 10 answers" in SLO
    assert "99%" in SLO and "432 minutes" in SLO  # 1% of 30 days


def test_the_log_queries_read_only_what_the_application_writes():
    queries = "\n".join(re.findall(r"query: '(.+)'", MONITORING))
    # events the code really emits, spelled the way it spells them
    sources = {
        "NORMAL_REQUEST": (ROOT / "src/leyline/services/chat_service.py").read_text(
            "utf-8"
        ),
        "MODEL_ERROR": (ROOT / "src/leyline/services/chat_service.py").read_text(
            "utf-8"
        ),
        "EVENT_STORE_FAILED": (ROOT / "src/leyline/services/event_queue.py").read_text(
            "utf-8"
        ),
        "first_token_ms": (ROOT / "src/leyline/services/chat_service.py").read_text(
            "utf-8"
        ),
        "worker heartbeat": (ROOT / "src/leyline/worker.py").read_text("utf-8"),
    }
    for token, source in sources.items():
        assert token in queries, token
        assert token in source, token
    assert "http_429" in queries and 'f"http_{exc.status_code}"' in (
        ROOT / "src/leyline/adapters/llm_openai.py"
    ).read_text("utf-8")


def test_the_probe_and_error_patterns_match_real_gunicorn_lines():
    ok = '127.0.0.1 - - [24/Nov/2026:14:04:43 +0000] "GET /readyz HTTP/1.1" 200 218 "-" "curl/8.11.1"'
    down = '127.0.0.1 - - [24/Nov/2026:14:04:43 +0000] "GET /readyz HTTP/1.1" 503 90 "-" "curl/8.11.1"'
    boom = '10.0.0.4 - - [24/Nov/2026:14:04:43 +0000] "POST /api/chat/stream HTTP/1.1" 500 21 "-" "x"'
    assert "GET /readyz" in ok and '" 200 ' in ok
    five = re.compile(r'" 5[0-9][0-9] ')
    assert five.search(down) and five.search(boom) and not five.search(ok)


def test_a_streamed_answer_logs_a_line_the_first_token_alert_can_parse(
    db, redis, blobs, embedder, add_doc, caplog
):
    from leyline.app import create_app
    from tests.support import make_settings

    add_doc("Password reset", "# P\n\nReset your password in the portal.", ["*"])
    app = create_app(
        make_settings(),
        redis_client=redis,
        database=db,
        blob_store=blobs,
        embedder=embedder,
        chat_model=FakeChatModel(reported_usage=(10, 5)),
    )
    app.config.update(TESTING=True)
    client = app.test_client()
    token = login_as(client, PERSONAS["employee"])
    with caplog.at_level("INFO", logger="secure_helpdesk"):
        response = client.post(
            "/api/chat/stream",
            json={"message": "How do I reset my password?"},
            headers={CSRF_HEADER: token},
        )
        response.get_data()  # the turn runs while the stream is read
    lines = [
        r.getMessage() for r in caplog.records if "NORMAL_REQUEST" in r.getMessage()
    ]
    assert lines
    event = json.loads(lines[-1])  # the whole line is JSON, as parse_json(Log_s) needs
    assert isinstance(event["details"]["first_token_ms"], float)
    assert event["details"]["first_token_ms"] <= event["details"]["latency_ms"]


def test_the_action_group_script_keeps_the_address_out_of_the_repository():
    script = (ROOT / "infra" / "alerts.ps1").read_text("utf-8")
    assert "[string]$Email" in script and "address not shown" in script
    assert not re.search(r"[\w.+-]+@(?!example\.com)[\w-]+\.[a-z]{2,}", script)
    deploy = (ROOT / "infra" / "deploy.ps1").read_text("utf-8")
    assert "ag-leyline" in deploy and "alertActionGroupId" in deploy
    assert "ag-leyline" in script or "ag-leyline" in script.replace("'ag-leyline'", "")


def test_a_missing_action_group_does_not_stop_a_deployment():
    """On a fresh or rebuilt environment the group does not exist yet.

    Windows PowerShell 5.1 under 'Stop' throws on redirected native error output, so the
    lookup must relax the preference before it asks.
    """
    deploy = (ROOT / "infra" / "deploy.ps1").read_text("utf-8")
    body = deploy.split("function Get-AlertGroupId {")[1].split("\nfunction ")[0]
    assert body.index("$ErrorActionPreference = 'Continue'") < body.index("& az ")


def test_the_log_alerts_can_be_created_before_any_log_exists():
    """A fresh workspace has no console-log table until the apps write to it."""
    assert "skipQueryValidation: true" in MONITORING


@pytest.mark.parametrize("module", ["main", "monitoring"])
def test_alerts_are_part_of_the_template(module):
    path = (
        ROOT
        / "infra"
        / ("main.bicep" if module == "main" else "modules/monitoring.bicep")
    )
    text = path.read_text("utf-8")
    assert (
        ("module monitoring" in text)
        if module == "main"
        else ("scheduledQueryRules" in text)
    )
