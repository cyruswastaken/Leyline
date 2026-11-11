"""Guards for the Azure infrastructure code (infra/): no stored secrets, nothing that can
delete the shared OpenAI group, and the app's environment lines up with the settings the
code reads. The Bicep itself is compiled by CI when the compiler is available."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from leyline.config import Settings

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra"
MODULES = INFRA / "modules"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def bicep_files() -> list[Path]:
    return [INFRA / "main.bicep", *sorted(MODULES.glob("*.bicep"))]


def test_secret_parameters_are_marked_secure_everywhere():
    for path in bicep_files():
        text = read(path)
        for match in re.finditer(
            r"^(@secure\(\)\s*\n)?param (\w+) string", text, re.MULTILINE
        ):
            name = match.group(2)
            if re.search(r"(Password|Secret|Key)$", name):
                assert match.group(1), f"{path.name}: param {name} must be @secure()"


def test_no_credential_is_written_in_a_template_or_script():
    pattern = re.compile(
        r"(AccountKey=[A-Za-z0-9+/=]{20,}|-----BEGIN|password\s*[:=]\s*['\"][^'\"$]{6,}['\"])",
        re.IGNORECASE,
    )
    for path in [*bicep_files(), INFRA / "deploy.ps1", INFRA / "teardown.ps1"]:
        assert not pattern.search(read(path)), path.name


def test_the_apps_read_every_secret_from_key_vault_not_from_values():
    apps = read(MODULES / "apps.bicep")
    assert "keyVaultUrl" in apps and "identity: identityId" in apps
    # An environment variable that carries a secret must reference it, never hold it.
    for name in (
        "DATABASE_URL",
        "REDIS_URL",
        "BLOB_CONNECTION_STRING",
        "LLM_API_KEY",
        "EMBEDDING_API_KEY",
        "OIDC_CLIENT_SECRET",
    ):
        assert re.search(rf"name: '{name}', secretRef: '[\w-]+'", apps), name


def test_key_vault_and_registry_use_identities_not_shared_secrets():
    platform = read(MODULES / "platform.bicep")
    assert "adminUserEnabled: false" in platform
    assert "enableRbacAuthorization: true" in platform
    assert "allowBlobPublicAccess: false" in platform
    assert "minimumTlsVersion: 'TLS1_2'" in platform


def test_the_database_connection_uses_tls():
    assert "ssl_context=true" in read(MODULES / "secrets.bicep")


def test_the_disposable_resources_live_in_their_own_group_apart_from_openai():
    main = read(INFRA / "main.bicep")
    assert "param appResourceGroupName string = 'rg-leyline-app'" in main
    assert "param openAiResourceGroupName string = 'rg-leyline'" in main
    # Only model deployments are created in the OpenAI group.
    openai = read(MODULES / "openai.bicep")
    assert "accounts@2024-10-01' existing" in openai
    assert "accounts@2024-10-01' =" not in openai


def test_teardown_deletes_only_the_app_group_unless_models_are_requested():
    script = read(INFRA / "teardown.ps1")
    deletes = re.findall(r"az group delete --name (\S+)", script)
    assert deletes == ["$AppResourceGroup"]
    assert "[string]$AppResourceGroup = 'rg-leyline-app'" in script
    # Removing the model deployments is opt-in.
    assert "if ($IncludeModels)" in script
    # A deleted vault keeps its name for days; the script must purge it.
    assert "az keyvault purge" in script
    # Confirmation is required unless -Force is passed.
    assert "Read-Host" in script and "-not $Force" in script


def test_deploy_never_writes_secrets_into_the_repository_or_a_command_line():
    script = read(INFRA / "deploy.ps1")
    assert "GetTempPath()" in script and "icacls" in script
    assert "-WhatIf:$false" in script  # a preview run must still clean its temp file
    assert '--parameters "@$paramFile"' in script
    assert not re.search(r"--parameters [^@\"]*[Pp]assword=", script)


def test_the_app_environment_sets_only_settings_the_code_understands():
    apps = read(MODULES / "apps.bicep")
    names = set(re.findall(r"name: '([A-Z][A-Z0-9_]+)'", apps))
    known = {field.upper() for field in Settings.model_fields}
    assert names, "no environment variables found"
    assert names <= known, f"unknown settings: {sorted(names - known)}"


def test_every_container_gets_the_settings_production_validation_requires(monkeypatch):
    """The web app, the worker and both jobs all load settings in production mode, so
    each must carry the required ones (a worker without SESSION_COOKIE_SECURE crash-loops)."""
    apps = read(MODULES / "apps.bicep")
    common = apps.split("var commonEnv = [", 1)[1].split("resource web", 1)[0]
    declared = dict(
        re.findall(r"name: '([A-Z0-9_]+)', (?:value: '([^']*)'|secretRef)", common)
    )
    names = set(re.findall(r"name: '([A-Z0-9_]+)'", common))
    # no container gets a private environment: all three use commonEnv and nothing else
    assert apps.count("env: commonEnv") == 4  # web, worker, task job, seed job
    assert "webEnv" not in apps
    # build a production configuration from exactly what the template injects
    placeholders = {
        "DATABASE_URL": "postgresql+pg8000://u:p@h:5432/leyline?ssl_context=true",
        "BLOB_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=k",
        "REDIS_URL": "rediss://:k@h:10000/0",
        "LLM_BASE_URL": "https://example.openai.azure.com/openai/v1/",
        "LLM_MODEL": "gpt-5.4-mini",
        "LLM_API_KEY": "k",
        "EMBEDDING_BASE_URL": "https://example.openai.azure.com/openai/v1/",
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "EMBEDDING_API_KEY": "k",
        "OIDC_METADATA_URL": "https://login.microsoftonline.com/t/v2.0/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": "id",
        "OIDC_GROUP_NAMES": "{}",
        "OIDC_CLIENT_SECRET": "s",
    }
    for name in names:
        monkeypatch.setenv(name, declared.get(name) or placeholders[name])
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # raises if anything required is missing
    assert settings.session_cookie_secure is True
    assert settings.llm_send_temperature is False
    assert settings.embedding_send_dimensions is True
    assert settings.embeddings_configured


def find_bicep() -> str | None:
    on_path = shutil.which("bicep")
    if on_path:
        return on_path
    # `az bicep install` puts it here.
    for name in ("bicep.exe", "bicep"):
        candidate = Path.home() / ".azure" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return None


@pytest.mark.skipif(find_bicep() is None, reason="the Bicep compiler is not installed")
def test_every_template_compiles_without_warnings():
    result = subprocess.run(  # noqa: S603
        [find_bicep() or "bicep", "build", str(INFRA / "main.bicep"), "--stdout"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Warning" not in result.stderr, result.stderr


# --- Entra registration (infra/entra.ps1) ----------------------------------------------------


def test_the_entra_script_never_prints_or_stores_the_client_secret_in_plain_text():
    script = read(INFRA / "entra.ps1")
    assert "ConvertFrom-SecureString" in script  # encrypted for the Windows account
    assert not re.search(r"Write-Host[^\n]*secretText", script)
    assert not re.search(r"Write-Host[^\n]*clientSecret", script)


def test_entra_local_files_are_git_ignored():
    ignore = read(ROOT / ".gitignore")
    assert "infra/.entra.local.json" in ignore
    assert "infra/.entra-demo-users.local.txt" in ignore


def test_the_entra_script_leaves_the_older_leyline_registration_alone():
    script = read(INFRA / "entra.ps1")
    assert "[string]$AppName = 'Leyline Helpdesk'" in script


def test_the_entra_roles_are_exactly_the_apps_roles_with_fixed_ids():
    from leyline.auth.principal import AppRole

    script = read(INFRA / "entra.ps1")
    declared = re.findall(
        r"id = '([0-9a-f-]{36})'; value = '(\w+)'", script, re.IGNORECASE
    )
    assert sorted(value for _, value in declared) == sorted(r.value for r in AppRole)
    assert len({guid for guid, _ in declared}) == len(declared)  # ids are unique


def test_only_assigned_users_may_sign_in_and_roles_cannot_be_invented_by_users():
    script = read(INFRA / "entra.ps1")
    assert "appRoleAssignmentRequired = $true" in script
    assert "signInAudience         = 'AzureADMyOrg'" in script  # this tenant only


def test_the_document_groups_match_the_demo_corpus_groups():
    script = read(INFRA / "entra.ps1")
    declared = re.search(r"\$groupNames = @\(([^)]*)\)", script)
    assert declared
    names = set(re.findall(r"'(\w+)'", declared.group(1)))
    corpus_groups: set[str] = set()
    for document in (ROOT / "corpus").rglob("*.md"):
        match = re.search(r"^Groups:\s*(.+)$", read(document), re.MULTILINE)
        if match:
            corpus_groups |= {g.strip() for g in match.group(1).split(",")}
    corpus_groups -= {"everyone", "*"}
    assert corpus_groups <= names, sorted(corpus_groups - names)


def test_deploy_uses_the_saved_entra_values_and_passes_the_group_map():
    script = read(INFRA / "deploy.ps1")
    assert (
        ".entra.local.json" in script
        and "ConvertTo-SecureString $saved.clientSecret" in script
    )
    assert "$second.oidcGroupNames = $oidcGroupNames" in script
    assert "OIDC_GROUP_NAMES" in read(MODULES / "apps.bicep")


def test_the_budget_script_keeps_the_email_out_of_the_repository_and_only_alerts():
    script = read(INFRA / "budget.ps1")
    assert "[string]$Email" in script  # supplied by the caller, not stored
    assert not re.search(
        r"[\w.+-]+@(gmail|outlook|hotmail)\.com", script, re.IGNORECASE
    )
    for percent in ("50", "80", "100"):
        assert f"New-Alert {percent} 'Actual'" in script
    assert "New-Alert 100 'Forecasted'" in script
    assert "-WhatIf:$false" in script  # a preview must still delete its temp file


def test_the_openai_key_is_read_after_the_deployments_in_its_own_module():
    # Reading the key while deployments are being written fails with RequestConflict.
    assert "listKeys" not in read(MODULES / "openai.bicep")
    assert "listKeys" in read(MODULES / "openai-key.bicep")
    main = read(INFRA / "main.bicep")
    block = main.split("module openaiKey", 1)[1].split("module secrets", 1)[0]
    assert "dependsOn: [openai]" in block


def test_the_environment_asks_for_the_standard_mode_not_express():
    # Without it this subscription creates an "express" environment, which has no Key Vault
    # secret references and no jobs; the apps need both.
    apps = read(MODULES / "apps.bicep")
    assert "environmentMode: 'WorkloadProfiles'" in apps
    assert (
        "'Express'"
        not in apps.split("resource environment", 1)[1].split("var identity", 1)[0]
    )
    assert re.search(
        r"managedEnvironments@(\d{4}-\d\d-\d\d)' = if \(deployApps\)", apps
    )


def test_pass_two_registers_the_sign_in_address_it_just_created():
    deploy = read(INFRA / "deploy.ps1")
    assert "entra.ps1" in deploy and "-RedirectUri $redirect" in deploy


def test_the_migration_job_runs_its_own_command_without_overrides():
    # az splits `--args '-m leyline.db upgrade'` into one bad argument, so the job's own
    # command (see apps.bicep) is the migration and the script overrides nothing.
    deploy = read(INFRA / "deploy.ps1")
    assert "job start --name leyline-task" in deploy
    assert "--args" not in deploy.split("job start", 1)[1].split("\n", 1)[0]
    assert "'leyline.db', 'upgrade'" in read(MODULES / "apps.bicep")


def test_the_logs_script_needs_no_cli_extension_and_validates_its_input():
    script = read(INFRA / "logs.ps1")
    assert "api.loganalytics.io" in script
    assert "containerapp job logs" not in script  # that command needs an extension
    assert "'^[A-Za-z0-9-]+$'" in script  # the app name goes into a query


def test_the_seed_job_runs_the_seed_as_its_own_command():
    apps = read(MODULES / "apps.bicep")
    block = apps.split("resource seed ", 1)[1].split("output defaultDomain", 1)[0]
    assert "command: ['python', '-m', 'leyline.seed']" in block
    assert "env: commonEnv" in block  # same production settings as every container
    deploy = read(INFRA / "deploy.ps1")
    assert "[switch]$Seed" in deploy and "job start --name leyline-seed" in deploy
    assert (
        "--args"
        not in deploy.split("job start --name leyline-seed", 1)[1].split("\n", 1)[0]
    )


def test_the_logs_script_can_hide_the_health_probes():
    script = read(INFRA / "logs.ps1")
    assert "[switch]$NoProbes" in script and "!has '/healthz'" in script


def test_the_entra_script_retries_reports_and_verifies_assignments():
    # Assigning a group or role to a user created a moment ago can fail at first; the
    # old script swallowed that silently, leaving six demo users with no role (sign-in
    # requires one) and no group.
    script = read(INFRA / "entra.ps1")
    assert "function Invoke-GraphRetry" in script
    assert "catch { Write-Verbose" not in script  # no silent swallowing any more
    assert "Verifying the demo users" in script and "exit 1" in script
    assert 'Invoke-GraphRetry "membership of' in script
    assert 'Invoke-GraphRetry "role' in script


def test_demo_passwords_can_be_reset_without_ever_printing_them():
    script = read(INFRA / "entra.ps1")
    assert "[switch]$ResetPasswords" in script and "function New-DemoPassword" in script
    assert script.count("RandomNumberGenerator") == 1  # one generator, not two copies
    assert not re.search(r"Write-(Host|Output|Warning)[^\n]*\$password", script)


def test_the_entra_script_keeps_redirect_addresses_that_are_already_registered():
    # Replacing the list removed the Azure sign-in address whenever the script ran without
    # -RedirectUri, and Microsoft then refused every Azure sign-in (redirect mismatch).
    script = read(INFRA / "entra.ps1")
    assert ".web.redirectUris" in script and "Select-Object -Unique" in script
    assert "@($registered) + @($redirects)" in script
