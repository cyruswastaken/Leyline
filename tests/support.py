import os
import tempfile

# A few modules read configuration when imported, so this must be set first.
# No real model or identity-provider calls are made in tests.
os.environ["LEYLINE_ENV"] = "test"
os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="leyline-test-logs-")

from leyline.auth.principal import AppRole, Principal
from leyline.config import Settings

CSRF_HEADER = "X-CSRF-Token"

PERSONAS: dict[str, Principal] = {
    "employee": Principal(
        user_id="u-employee",
        name="Eve Employee",
        groups=frozenset({"staff"}),
    ),
    "security": Principal(
        user_id="u-security",
        name="Sam Security",
        groups=frozenset({"staff", "security"}),
    ),
    "auditor": Principal(
        user_id="u-auditor",
        name="Ada Auditor",
        groups=frozenset({"staff"}),
        roles=frozenset({AppRole.USER, AppRole.SECURITY_AUDITOR}),
    ),
    "kb_admin": Principal(
        user_id="u-kbadmin",
        name="Kim Admin",
        groups=frozenset({"staff"}),
        roles=frozenset({AppRole.USER, AppRole.KB_ADMIN}),
    ),
    "support_agent": Principal(
        user_id="u-agent",
        name="Sue Agent",
        groups=frozenset({"staff"}),
        roles=frozenset({AppRole.USER, AppRole.SUPPORT_AGENT}),
    ),
}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "leyline_env": "test",
        "azure_openai_endpoint": "https://example.invalid/",
        "azure_openai_api_key": "test-key",
        "azure_openai_deployment": "test-deployment",
        "oidc_metadata_url": "https://idp.invalid/.well-known/openid-configuration",
        "oidc_client_id": "leyline-web",
        "oidc_client_secret": "test-secret",
        "log_dir": os.environ["LOG_DIR"],
        "rate_limit_ip_requests": 1000,
    }
    values.update(overrides)
    # _env_file=None: never let a developer's local .env change test behavior.
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def login_as(client, principal: Principal) -> str:
    """Create a real server-side session for ``principal``; return its CSRF token."""
    token = f"csrf-{principal.user_id}"
    with client.session_transaction() as session:
        session["principal"] = principal.to_session()
        session["csrf_token"] = token
    return token
