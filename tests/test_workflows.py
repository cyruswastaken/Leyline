"""Guards on the GitHub Actions workflows and the secret-scanner configuration.

The repository is public and a pipeline holds the keys to the deployment, so what the
workflows run is part of the security boundary: third-party actions must be pinned to an
exact commit, and the secret scanner's exceptions must stay as narrow as they are.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
FIRST_PARTY = ("actions/", "github/")


def uses_lines(path: Path) -> list[str]:
    return re.findall(r"^\s*-?\s*uses:\s*(\S+)", path.read_text(encoding="utf-8"), re.M)


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_third_party_actions_are_pinned_to_a_full_commit_sha(workflow):
    for reference in uses_lines(workflow):
        if reference.startswith(("./",)) or reference.startswith(FIRST_PARTY):
            continue
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", reference), (
            f"{workflow.name}: {reference} must be pinned to a 40-character commit"
        )


def test_the_gitleaks_exceptions_are_only_the_two_public_azure_role_ids():
    config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    allowed = set(re.findall(r"'''([0-9a-f-]{36})'''", config))
    bicep = (ROOT / "infra" / "modules" / "platform.bicep").read_text(encoding="utf-8")
    role_ids = set(re.findall(r"Role = '([0-9a-f-]{36})'", bicep))
    assert allowed == role_ids and len(allowed) == 2
    # no wildcard-style regex exceptions, only exact ids
    regexes = config.split("regexes = [", 1)[1].split("]", 1)[0]
    assert not re.search(
        r"[.*+?\[\]\|(){}]", regexes.replace("'''", "").replace("-", "")
    )


def test_the_secret_scan_covers_the_whole_history():
    security = (ROOT / ".github" / "workflows" / "security.yml").read_text(
        encoding="utf-8"
    )
    assert "fetch-depth: 0" in security and "gitleaks" in security


# --- the Azure deploy workflow and its identity script ------------------------------------------

import yaml  # noqa: E402  (kept next to the tests that use it)

DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"
IDENTITY = ROOT / "infra" / "ci-identity.ps1"


def deploy_workflow() -> dict:
    return yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))


def triggers(workflow: dict) -> dict:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def test_the_deploy_workflow_can_only_be_started_by_hand():
    assert set(triggers(deploy_workflow())) == {"workflow_dispatch"}
    text = DEPLOY.read_text(encoding="utf-8")
    for dangerous in (
        "pull_request_target",
        "pull_request:",
        "push:",
        "schedule:",
        "workflow_run",
    ):
        assert dangerous not in text.split("jobs:", 1)[1], dangerous


def test_the_preview_is_the_default_and_deploying_is_a_deliberate_choice():
    inputs = triggers(deploy_workflow())["workflow_dispatch"]["inputs"]
    assert inputs["action"]["default"] == "preview"
    assert inputs["action"]["options"] == ["preview", "deploy"]
    assert inputs["seed"]["default"] is False


def test_nothing_reaches_azure_without_the_gate_and_the_approval_environment():
    jobs = deploy_workflow()["jobs"]
    assert jobs["azure"]["needs"] == "gate"
    assert (
        jobs["azure"]["environment"] == "azure"
    )  # required reviewer, main branch only
    text = DEPLOY.read_text(encoding="utf-8")
    assert 'if [ "$REF" != "refs/heads/main" ]' in text
    # CI and Security must have succeeded for this exact commit
    assert "for workflow in CI Security" in text and '--commit "$SHA"' in text


def test_the_pipeline_holds_no_stored_secret_and_no_hardcoded_azure_ids():
    text = DEPLOY.read_text(encoding="utf-8")
    assert (
        "secrets." not in text
    )  # login is by OIDC; github.token is not a stored secret
    for variable in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"):
        assert f"vars.{variable}" in text
    assert not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text
    )
    assert "--password" not in text and "client-secret" not in text


def test_only_the_azure_job_may_request_an_identity_token():
    workflow = deploy_workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert "id-token" not in workflow["jobs"]["gate"]["permissions"]
    assert workflow["jobs"]["azure"]["permissions"]["id-token"] == "write"


def test_untrusted_values_never_reach_a_shell_script_directly():
    # Inputs and refs go through env: so they cannot become shell syntax.
    for job in deploy_workflow()["jobs"].values():
        for step in job["steps"]:
            script = step.get("run", "")
            assert "${{" not in script, (
                f"expression inside a run script: {step.get('name')}"
            )


def test_the_deploy_updates_a_running_environment_and_never_deletes_or_touches_entra():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "az group delete" not in text and "teardown" not in text
    assert "az ad " not in text and "graph.microsoft.com" not in text
    assert (
        "writeOidcSecret:{value:false}" in text
    )  # leaves the stored sign-in secret alone
    assert (
        "leyline-web does not exist" in text
    )  # refuses to create an environment from nothing
    assert "::add-mask::" in text  # the generated database password is masked


def test_the_template_has_the_switch_the_pipeline_relies_on():
    main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
    assert "param writeOidcSecret bool = true" in main
    assert "includeOidcSecret: deployApps && writeOidcSecret" in main


def test_the_identity_trusts_exactly_one_environment_of_this_repository():
    script = IDENTITY.read_text(encoding="utf-8")
    assert "environment:$EnvironmentName" in script
    assert "[string]$Repository = 'cyruswastaken/Leyline'" in script
    assert "[string]$EnvironmentName = 'azure'" in script
    assert "api://AzureADTokenExchange" in script
    # no wildcard or branch-based subject anywhere
    assert (
        ":ref:" not in script
        and "pull_request" not in script
        and "*" not in script.split("$subject =", 1)[1].split("\n", 1)[0]
    )


def test_the_identity_cannot_delete_the_environment_or_widen_its_own_rights():
    script = IDENTITY.read_text(encoding="utf-8")
    for denied in (
        "Microsoft.Resources/subscriptions/resourceGroups/delete",
        "Microsoft.Authorization/*/Write",
        "Microsoft.Authorization/elevateAccess/Action",
    ):
        assert denied in script
    # the starter role only starts deployments and writes resource groups: no delete action
    block = script.split("custom role '$starterName' (subscription)", 1)[1].split(
        "custom role '$deployerName'", 1
    )[0]
    actions = re.search(r"actions = @\(([^)]*)\)", block).group(1)
    assert "Microsoft.Resources/deployments/*" in actions
    assert "delete" not in actions.lower() and "Authorization" not in actions
    # no Owner, no User Access Administrator, no plain Contributor
    for broad in (
        "Owner",
        "User Access Administrator",
        "b24988ac-6180-42a0-ab88-20f7382dd24c",
    ):
        assert (
            broad
            not in script.split("$RoleRbacAdministrator =", 1)[1].split(
                "function Invoke-Rest", 1
            )[0]
        )


def test_the_role_assignment_condition_allows_only_the_roles_the_template_assigns():
    script = IDENTITY.read_text(encoding="utf-8")
    listed = set(
        re.findall(
            r"'([0-9a-f-]{36})'",
            script.split("$AssignableByCi = @(", 1)[1].split(")", 1)[0],
        )
    )
    bicep = (ROOT / "infra" / "modules" / "platform.bicep").read_text(encoding="utf-8")
    used = set(re.findall(r"Role = '([0-9a-f-]{36})'", bicep))
    assert listed == used and len(listed) == 2
    assert (
        "GuidEquals {$ids}" in script and script.count("GuidEquals {$ids}") == 2
    )  # write and delete


def test_the_identity_script_previews_and_is_scoped_to_what_it_owns():
    script = IDENTITY.read_text(encoding="utf-8")
    assert "SupportsShouldProcess" in script and "[switch]$Remove" in script
    assert "client secret" not in script.lower().replace(
        "no password, key or secret", ""
    )
    assert (
        "az ad app credential" not in script and "addPassword" not in script
    )  # creates no secret
    assert "environment: azure" not in script  # naming only through the parameter
