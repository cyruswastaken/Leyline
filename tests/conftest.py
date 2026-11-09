import fakeredis
import pytest

from leyline.app import create_app
from tests.support import PERSONAS, login_as, make_settings  # sets test env first


@pytest.fixture
def redis():
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def app(settings, redis, llm_calls):
    flask_app = create_app(settings, redis_client=redis)
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def llm_calls(monkeypatch):
    """Replace the model call and record the context it would receive."""
    calls = []

    def fake_generate_answer(query, context, source):
        calls.append({"query": query, "context": context, "source": source})
        return "stub answer"

    monkeypatch.setattr(
        "leyline.services.chat_service.generate_answer", fake_generate_answer
    )
    return calls


@pytest.fixture
def events(monkeypatch):
    """Record every security event instead of writing it to the log file."""
    recorded = []

    def record(**kwargs):
        recorded.append(kwargs)

    for target in (
        "leyline.services.chat_service",
        "leyline.web.auth",
        "leyline.auth.access",
        "leyline.app",
    ):
        monkeypatch.setattr(f"{target}.log_security_event", record)
    return recorded


@pytest.fixture
def login(client):
    def _login(persona: str) -> str:
        return login_as(client, PERSONAS[persona])

    return _login
