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
    "database_url": "postgresql+pg8000://u:p@db.invalid:5432/leyline",
    "blob_connection_string": "AccountName=x;AccountKey=y",
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
        ({"database_url": None}, "DATABASE_URL"),
        ({"blob_connection_string": None}, "BLOB_CONNECTION_STRING"),
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


def test_chunk_overlap_must_be_smaller_than_chunk_size_in_every_environment():
    # Otherwise the chunker could never advance through a document.
    for env in ("local", "test", "production"):
        with pytest.raises(ValidationError, match="CHUNK_OVERLAP_WORDS"):
            settings(leyline_env=env, chunk_target_words=100, chunk_overlap_words=100)


def test_credentials_are_secret_strings():
    cfg = settings()
    text = repr(cfg)
    assert "postgresql" not in text and "AccountKey" not in text
    assert cfg.database_url is not None
    assert "p@" not in str(cfg.database_url)  # str() masks it too


def test_embeddings_are_optional_and_need_both_url_and_model():
    assert settings().embeddings_configured is False
    assert settings(embedding_base_url="http://x/v1").embeddings_configured is False
    assert settings(embedding_model="m").embeddings_configured is False
    both = settings(embedding_base_url="http://x/v1", embedding_model="m")
    assert both.embeddings_configured is True


def test_feature_flags_reflect_configuration():
    loose = Settings(_env_file=None, leyline_env="local")
    assert loose.database_configured is False
    assert loose.blob_configured is False
    assert settings().database_configured and settings().blob_configured


def test_configuration_errors_never_echo_secret_values():
    secrets = {
        "database_url": "postgresql+pg8000://admin:hunter2-db-pass@db.invalid/x",
        "oidc_client_secret": "hunter2-oidc-secret",
        "azure_openai_api_key": "hunter2-openai-key",
        "blob_connection_string": "AccountName=x;AccountKey=hunter2-blob-key",
    }
    with pytest.raises(ValidationError) as failure:
        settings(flask_debug=True, **secrets)  # invalid: debug in production
    rendered = str(failure.value)
    assert "FLASK_DEBUG" in rendered
    for value in secrets.values():
        assert value not in rendered
    assert "hunter2" not in rendered
