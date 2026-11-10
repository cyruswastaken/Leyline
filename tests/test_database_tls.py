"""The database URL's ssl_context option becomes a real TLS setting.

Azure Database for PostgreSQL requires TLS. SQLAlchemy passes URL options to the
driver as text, and pg8000 then fails with "'str' object has no attribute
'wrap_socket'", so the option is translated in one place (Database).
"""

import pytest

from leyline.db import database as database_module
from leyline.db.database import Database, _split_tls_option

BASE = "postgresql+pg8000://leyline:p%40ss@host.example:5432/leyline"


@pytest.fixture
def captured(monkeypatch):
    calls = {}
    real = database_module.create_engine

    def spy(url, **kwargs):
        calls["url"] = url
        calls["connect_args"] = kwargs["connect_args"]
        return real(url, **kwargs)

    monkeypatch.setattr(database_module, "create_engine", spy)
    return calls


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_a_true_ssl_context_becomes_the_boolean_the_driver_needs(captured, value):
    Database(f"{BASE}?ssl_context={value}").dispose()
    assert captured["connect_args"]["ssl_context"] is True
    assert "ssl_context" not in captured["url"]  # no longer a text option on the URL


def test_the_rest_of_the_url_survives_including_an_encoded_password(captured):
    Database(f"{BASE}?ssl_context=true").dispose()
    assert captured["url"] == BASE  # password stays percent-encoded


@pytest.mark.parametrize("value", ["false", "0", "no"])
def test_a_false_ssl_context_means_no_tls_option(captured, value):
    Database(f"{BASE}?ssl_context={value}").dispose()
    assert "ssl_context" not in captured["connect_args"]


def test_without_the_option_nothing_changes(captured):
    Database(BASE).dispose()
    assert "ssl_context" not in captured["connect_args"]
    assert captured["url"] == BASE


def test_a_garbled_value_is_refused_not_ignored():
    with pytest.raises(ValueError, match="ssl_context"):
        _split_tls_option(f"{BASE}?ssl_context=maybe")


def test_other_url_options_are_kept():
    url, tls = _split_tls_option(f"{BASE}?ssl_context=true&application_name=leyline")
    assert tls == {"ssl_context": True}
    assert "application_name=leyline" in url and "ssl_context" not in url
