import os

import pytest

# The LLM module validates configuration at import time, so dummy values
# must exist before the app is imported. No real calls are made in tests.
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://example.invalid/")
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key")
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "test-deployment")


@pytest.fixture
def llm_calls(monkeypatch):
    """Replace the model call and record the context it would receive."""
    calls = []

    def fake_generate_answer(query, context, source):
        calls.append({"query": query, "context": context, "source": source})
        return "stub answer"

    monkeypatch.setattr(
        "services.chat_service.generate_answer", fake_generate_answer
    )
    return calls


@pytest.fixture
def client(llm_calls, monkeypatch, tmp_path):
    from services.rate_limiter import rate_limiter

    rate_limiter.requests.clear()
    # Keep test events out of the real security log.
    monkeypatch.setattr(
        "services.chat_service.log_security_event", lambda **kwargs: None
    )

    from app import app

    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client
    rate_limiter.requests.clear()
