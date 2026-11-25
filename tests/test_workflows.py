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


def azure_steps() -> dict[str, dict]:
    return {s.get("name", ""): s for s in deploy_workflow()["jobs"]["azure"]["steps"]}


def test_a_destructive_migration_is_refused_unless_it_was_ticked():
    inputs = triggers(deploy_workflow())["workflow_dispatch"]["inputs"]
    assert inputs["allow_destructive_migration"]["default"] is False
    step = azure_steps()["Check the migrations can be rolled back over"]
    script = step["run"]
    assert "scripts/check_migrations.py" in script and "--since" in script
    assert "--allow" in script  # only added when the box was ticked
    assert 'INPUT_ALLOW_DESTRUCTIVE_MIGRATION" = "true"' in script
    # the check runs before anything is built or changed in Azure
    names = list(azure_steps())
    assert names.index("Check the migrations can be rolled back over") < names.index(
        "Deploy the apps"
    )
    checkout = deploy_workflow()["jobs"]["azure"]["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0  # it compares with the live commit


def test_the_deploy_smoke_tests_the_site_and_rolls_back_a_failed_release():
    steps = azure_steps()
    smoke = steps["Smoke test the deployed site (signed out)"]
    assert "scripts/smoke_test.py" in smoke["run"] and smoke["id"] == "smoke"
    rollback = steps["Roll back to the previous image (the release failed its checks)"]
    condition = rollback["if"]
    assert "failure()" in condition and "steps.deploy.outcome == 'success'" in condition
    script = rollback["run"]
    assert '--image "$PREVIOUS_WEB_IMAGE"' in script
    assert '--image "$PREVIOUS_WORKER_IMAGE"' in script
    # it keeps the job red so a rolled-back release is never mistaken for a deploy
    assert script.rstrip().endswith("exit 1")
    # the order: ready, then smoke, then rollback
    names = list(steps)
    assert names.index("Smoke test the deployed site (signed out)") < names.index(
        "Roll back to the previous image (the release failed its checks)"
    )


def test_the_previous_images_are_remembered_before_anything_changes():
    steps = azure_steps()
    read = steps["Read the running environment (this workflow only updates it)"]["run"]
    assert "PREVIOUS_WEB_IMAGE" in read and "PREVIOUS_WORKER_IMAGE" in read
    names = list(steps)
    assert names.index(
        "Read the running environment (this workflow only updates it)"
    ) < names.index("Deploy the apps")


def test_an_update_never_switches_how_the_environment_signs_in_to_its_data_services():
    text = DEPLOY.read_text(encoding="utf-8")
    # read from the running app, passed back to the template, never stored in GitHub
    assert (
        "DATABASE_ENTRA_AUTH" in text
        and 'useManagedIdentity:{value:($mi == "true")}' in text
    )
    assert "alertActionGroupId:{value:$ag}" in text
    assert "vars.USE_MANAGED_IDENTITY" not in text and "vars.ALERT" not in text


def test_the_template_has_the_switch_the_pipeline_relies_on():
    main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
    assert "param writeOidcSecret bool = true" in main
    assert "includeOidcSecret: deployApps && writeOidcSecret" in main


def test_the_identity_trusts_exactly_one_environment_of_this_repository():
    script = IDENTITY.read_text(encoding="utf-8")
    assert "environment:$EnvironmentName" in script
    # the prefix comes from GitHub (it is by id when immutable subjects are on), and is validated
    assert "actions/oidc/customization/sub" in script and "sub_claim_prefix" in script
    assert "-notmatch '^repo:[A-Za-z0-9._@/-]+$'" in script
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
    used = set(re.findall(r"(?:Role|Id) = '([0-9a-f-]{36})'", bicep))
    assert (
        listed == used and len(listed) == 3
    )  # AcrPull, Key Vault Secrets User, Blob Data Contributor
    assert (
        "GuidEquals {$ids}" in script and script.count("GuidEquals {$ids}") == 2
    )  # write and delete


def test_on_the_openai_account_only_the_openai_user_role_may_be_assigned():
    script = IDENTITY.read_text(encoding="utf-8")
    listed = set(
        re.findall(
            r"'([0-9a-f-]{36})'",
            script.split("$AssignableOnOpenAi = @(", 1)[1].split(")", 1)[0],
        )
    )
    access = (ROOT / "infra" / "modules" / "openai-access.bicep").read_text(
        encoding="utf-8"
    )
    used = set(re.findall(r"Id = '([0-9a-f-]{36})'", access))
    assert listed == used and len(listed) == 1
    assert script.count("GuidEquals {$openAiIds}") == 2  # write and delete
    # the conditioned right is the only role-assignment right on that account
    assert script.count("$openAiScope $RoleRbacAdministrator") == 1
    assert (
        "$openAiCondition"
        in script.split("$openAiScope $RoleRbacAdministrator", 1)[1][:200]
    )


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
