import os
import tempfile

import pytest

# Configuration is read when modules are imported, so test values must be
# in the environment first. No real model calls are made in tests.
os.environ["LEYLINE_ENV"] = "test"
os.environ["AZURE_OPENAI_ENDPOINT"] = "https://example.invalid/"
os.environ["AZURE_OPENAI_API_KEY"] = "test-key"
os.environ["AZURE_OPENAI_DEPLOYMENT"] = "test-deployment"
os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="leyline-test-logs-")


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
def client(llm_calls, monkeypatch):
    from leyline.app import create_app
    from leyline.services.rate_limiter import rate_limiter

    rate_limiter.requests.clear()

    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client
    rate_limiter.requests.clear()
