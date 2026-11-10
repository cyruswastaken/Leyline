"""Chat completions through any OpenAI-compatible endpoint.

One adapter serves Azure OpenAI (v1 API), OpenAI and local servers such as
Ollama (``http://localhost:11434/v1``): only base URL, key and model differ.
Failures are mapped to ``ModelUnavailableError`` and guarded by a circuit
breaker so an outage degrades the answer instead of breaking the request.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import openai
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from leyline.adapters.circuit_breaker import CircuitBreaker
from leyline.ports.llm import ModelUnavailableError

logger = logging.getLogger(__name__)

# Whatever the SDK or its transport raises (API errors, dropped connections,
# malformed payloads) means "the model is unavailable"; GeneratorExit from a
# cancelled stream is a BaseException and is deliberately not caught.
_PROVIDER_ERRORS = Exception


class OpenAICompatibleChatModel:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30,
        max_retries: int = 2,
        temperature: float = 0.1,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client = OpenAI(
            base_url=base_url,
            # Local servers ignore the key but the SDK requires a value.
            api_key=api_key or "not-needed",
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        self._model = model
        self._temperature = temperature
        self._breaker = breaker or CircuitBreaker()

    @property
    def model(self) -> str:
        return self._model

    def _messages(self, system: str, user: str) -> list[ChatCompletionMessageParam]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _guard(self) -> None:
        if not self._breaker.allow():
            raise ModelUnavailableError("circuit_open")

    def _failed(self, exc: Exception) -> ModelUnavailableError:
        self._breaker.record_failure()
        if isinstance(exc, openai.APITimeoutError):
            reason = "timeout"
        elif isinstance(exc, openai.APIConnectionError):
            reason = "connection_error"
        elif isinstance(exc, openai.APIStatusError):
            reason = f"http_{exc.status_code}"
        else:
            reason = type(exc).__name__
        # The provider's message can echo request content; log the class only.
        logger.warning("chat model call failed: %s", reason)
        return ModelUnavailableError(reason)

    def complete(self, system: str, user: str) -> str:
        self._guard()
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=self._messages(system, user),
                temperature=self._temperature,
            )
        except _PROVIDER_ERRORS as exc:
            raise self._failed(exc) from exc
        text = response.choices[0].message.content if response.choices else None
        if not text or not text.strip():
            self._breaker.record_failure()
            raise ModelUnavailableError("empty_response")
        self._breaker.record_success()
        return text

    def stream(self, system: str, user: str) -> Iterator[str]:
        self._guard()
        produced = False
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=self._messages(system, user),
                temperature=self._temperature,
                stream=True,
            )
            try:
                for chunk in response:
                    if not chunk.choices:
                        continue
                    piece = chunk.choices[0].delta.content
                    if piece:
                        produced = True
                        yield piece
            finally:
                # Closing the generator (client cancelled) closes the HTTP stream.
                response.close()
        except _PROVIDER_ERRORS as exc:
            raise self._failed(exc) from exc
        if not produced:
            self._breaker.record_failure()
            raise ModelUnavailableError("empty_response")
        self._breaker.record_success()
