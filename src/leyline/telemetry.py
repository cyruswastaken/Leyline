"""Traces and metrics (OpenTelemetry), off unless an exporter is configured.

The rest of the code calls the small helpers here and never touches the SDK, so
telemetry costs nothing when disabled. What is recorded: stage names and
durations, outcomes, counts and *estimated* token counts. What is never
recorded: prompts, answers, document text, or user identifiers.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics, trace
from opentelemetry.metrics import Counter, Histogram, Meter, MeterProvider
from opentelemetry.trace import Span, Tracer, TracerProvider

if TYPE_CHECKING:
    from leyline.config import Settings

SERVICE = "leyline"

_tracer: Tracer
_meter: Meter
_requests: Counter
_outcomes: Counter
_feedback: Counter
_tokens: Counter
_stage_ms: Histogram
_first_token_ms: Histogram
_turn_ms: Histogram
_http_ms: Histogram
_sdk_providers: list[Any] = []


def use_providers(
    tracer_provider: TracerProvider | None = None,
    meter_provider: MeterProvider | None = None,
) -> None:
    """Bind the instruments to these providers (no-ops when omitted)."""
    global _tracer, _meter, _requests, _outcomes, _feedback, _tokens
    global _stage_ms, _first_token_ms, _turn_ms, _http_ms
    _tracer = (tracer_provider or trace.NoOpTracerProvider()).get_tracer(SERVICE)
    _meter = (meter_provider or metrics.NoOpMeterProvider()).get_meter(SERVICE)
    _requests = _meter.create_counter(
        "leyline.http.requests", description="HTTP requests by route and status class"
    )
    _outcomes = _meter.create_counter(
        "leyline.chat.outcomes",
        description="Chat turns by outcome: answered, refused, model_error, blocked, "
        "rate_limited",
    )
    _feedback = _meter.create_counter(
        "leyline.feedback", description="Answer ratings (rating=1 or -1)"
    )
    _tokens = _meter.create_counter(
        "leyline.llm.tokens.estimated",
        description="Estimated tokens sent to and received from the chat model",
    )
    _stage_ms = _meter.create_histogram(
        "leyline.chat.stage.duration", unit="ms", description="Time in a pipeline stage"
    )
    _first_token_ms = _meter.create_histogram(
        "leyline.chat.first_token", unit="ms", description="Question to first text"
    )
    _turn_ms = _meter.create_histogram(
        "leyline.chat.turn.duration", unit="ms", description="Whole streamed turn"
    )
    _http_ms = _meter.create_histogram(
        "leyline.http.duration", unit="ms", description="HTTP request duration"
    )


use_providers()  # no-op until configure_telemetry()


def configure_telemetry(settings: Settings) -> None:
    """Install exporters according to ``OTEL_EXPORTER`` (none, console, otlp)."""
    if settings.otel_exporter == "none":
        return

    from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": settings.otel_service_name})
    if settings.otel_exporter == "console":
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        span_exporter = ConsoleSpanExporter()
        metric_exporter = ConsoleMetricExporter()
    else:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise RuntimeError(
                "OTEL_EXPORTER=otlp needs: pip install "
                "opentelemetry-exporter-otlp-proto-http"
            ) from exc
        base = (settings.otel_otlp_endpoint or "http://localhost:4318").rstrip("/")
        span_exporter = OTLPSpanExporter(endpoint=f"{base}/v1/traces")
        metric_exporter = OTLPMetricExporter(endpoint=f"{base}/v1/metrics")

    tracer_provider = SdkTracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    meter_provider = SdkMeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
    )
    use_providers(tracer_provider, meter_provider)
    _sdk_providers[:] = [tracer_provider, meter_provider]


def shutdown_telemetry() -> None:
    """Flush and stop exporters configured by ``configure_telemetry``."""
    for provider in _sdk_providers:
        provider.shutdown()
    _sdk_providers.clear()
    use_providers()


# --- helpers used by the application -----------------------------------------------


@contextmanager
def span(name: str, **attributes: str | int | float | bool) -> Iterator[Span]:
    with _tracer.start_as_current_span(name, attributes=attributes) as current:
        yield current


@contextmanager
def stage(name: str) -> Iterator[None]:
    """A pipeline stage: a child span plus a duration sample."""
    started = time.perf_counter()
    with span(f"chat.{name}"):
        try:
            yield
        finally:
            _stage_ms.record((time.perf_counter() - started) * 1000, {"stage": name})


def start_span(name: str) -> Span:
    """A span that is *not* made current: for work spread over a generator's
    yields, where entering and leaving a context could happen in different
    places. The caller must call ``end()``."""
    return _tracer.start_span(name)


def stage_duration(name: str, started: float) -> None:
    _stage_ms.record((time.perf_counter() - started) * 1000, {"stage": name})


def chat_outcome(outcome: str) -> None:
    _outcomes.add(1, {"outcome": outcome})


def first_token(started: float) -> None:
    _first_token_ms.record((time.perf_counter() - started) * 1000)


def turn_finished(started: float, outcome: str) -> None:
    _turn_ms.record((time.perf_counter() - started) * 1000, {"outcome": outcome})


def feedback(rating: int) -> None:
    _feedback.add(1, {"rating": rating})


def tokens(direction: str, estimate: int) -> None:
    _tokens.add(max(0, estimate), {"direction": direction})


def http_request(route: str, method: str, status: int, started: float) -> None:
    attrs = {"route": route, "method": method, "status_class": f"{status // 100}xx"}
    _requests.add(1, attrs)
    _http_ms.record((time.perf_counter() - started) * 1000, attrs)
