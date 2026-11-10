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


def test_the_production_environment_satisfies_production_validation(monkeypatch):
    """The settings the template injects must pass the app's own production checks."""
    env = {
        "LEYLINE_ENV": "production",
        "DATABASE_URL": "postgresql+pg8000://u:p@h:5432/leyline?ssl_context=true",
        "BLOB_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=k",
        "REDIS_URL": "rediss://:k@h:10000/0",
        "LLM_BASE_URL": "https://example.openai.azure.com/openai/v1/",
        "LLM_MODEL": "gpt-5.4-mini",
        "LLM_API_KEY": "k",
        "LLM_SEND_TEMPERATURE": "false",
        "EMBEDDING_BASE_URL": "https://example.openai.azure.com/openai/v1/",
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "EMBEDDING_API_KEY": "k",
        "EMBEDDING_SEND_DIMENSIONS": "true",
        "OIDC_METADATA_URL": "https://login.microsoftonline.com/t/v2.0/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": "id",
        "OIDC_CLIENT_SECRET": "s",
        "SESSION_COOKIE_SECURE": "true",
        "TRUSTED_PROXY_COUNT": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
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
