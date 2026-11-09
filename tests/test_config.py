import pytest
from pydantic import ValidationError

from leyline.config import Settings

FULL_PRODUCTION = {
    "leyline_env": "production",
    "azure_openai_endpoint": "https://example.invalid/",
    "azure_openai_api_key": "k",
    "azure_openai_deployment": "d",
    "oidc_metadata_url": "https://idp.invalid/.well-known/openid-configuration",
    "oidc_client_id": "leyline-web",
    "oidc_client_secret": "s",
    "session_cookie_secure": True,
}


def settings(**overrides):
    return Settings(_env_file=None, **{**FULL_PRODUCTION, **overrides})


def test_a_complete_production_configuration_is_accepted():
    assert settings().leyline_env == "production"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"flask_debug": True}, "FLASK_DEBUG"),
        ({"azure_openai_api_key": None}, "AZURE_OPENAI"),
        ({"azure_openai_endpoint": None}, "AZURE_OPENAI"),
        ({"azure_openai_deployment": None}, "AZURE_OPENAI"),
        ({"oidc_metadata_url": None}, "OIDC_"),
        ({"oidc_client_id": None}, "OIDC_"),
        ({"oidc_client_secret": None}, "OIDC_"),
        ({"session_cookie_secure": False}, "SESSION_COOKIE_SECURE"),
    ],
)
def test_production_refuses_to_start_when_unsafe_or_incomplete(override, message):
    with pytest.raises(ValidationError, match=message):
        settings(**override)


def test_local_and_test_environments_start_unconfigured():
    for env in ("local", "test"):
        loose = Settings(_env_file=None, leyline_env=env)
        assert loose.llm_configured is False
        assert loose.oidc_configured is False


@pytest.mark.parametrize(
    "field",
    [
        "rate_limit_requests",
        "rate_limit_ip_requests",
        "rate_limit_window_seconds",
        "session_idle_seconds",
        "session_absolute_seconds",
        "max_message_length",
    ],
)
def test_limits_must_be_positive(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_secrets_are_not_exposed_in_repr():
    text = repr(settings())
    assert "test-secret" not in text and "'s'" not in text
    assert "SecretStr" in text


def test_restricted_groups_parse_from_a_comma_separated_list():
    parsed = Settings(_env_file=None, restricted_access_groups=" security , hr ,, ")
    assert parsed.restricted_groups == {"security", "hr"}
    assert Settings(_env_file=None, restricted_access_groups="").restricted_groups == (
        frozenset()
    )
