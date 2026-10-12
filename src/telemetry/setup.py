"""OpenTelemetry tracer provider and exporter configuration."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from src.telemetry.jsonl_exporter import JsonlSpanExporter

logger = logging.getLogger(__name__)

_DEFAULT_JSONL_PATH = Path("artifacts/observability/otel-traces.jsonl")
_initialized = False
_provider: TracerProvider | None = None


def _resolve_jsonl_path(jsonl_path: str | Path | None) -> Path | None:
    if jsonl_path is not None:
        value = str(jsonl_path).strip()
    else:
        value = os.environ.get("AGENTFIXB_TRACE_JSONL_PATH", str(_DEFAULT_JSONL_PATH)).strip()
    if value.lower() in {"", "0", "false", "none", "off", "disabled"}:
        return None
    return Path(value)


def init_telemetry(
    *,
    service_name: str = "agentfixbpro",
    endpoint: str | None = None,
    console: bool = False,
    jsonl_path: str | Path | None = None,
) -> TracerProvider:
    """Initialize the global tracer provider with non-blocking exporters.

    Completed spans are queued through ``BatchSpanProcessor`` for local JSONL
    storage and, when configured, an OTLP collector such as Phoenix. Console
    export is opt-in because it runs synchronously and can add request latency.
    """
    global _initialized, _provider
    if _provider is not None:
        _initialized = True
        return _provider

    resource = Resource.create({"service.name": service_name})
    # Engineering traces are intentionally unsampled. ALWAYS_ON also
    # ignores an unsampled remote parent so every local Agent run is recorded.
    provider = TracerProvider(resource=resource, sampler=ALWAYS_ON)

    local_path = _resolve_jsonl_path(jsonl_path)
    if local_path is not None:
        provider.add_span_processor(BatchSpanProcessor(JsonlSpanExporter(local_path)))

    otlp_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    if console:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _provider = provider
    _initialized = True
    return provider


def get_tracer(name: str = "agentfixbpro") -> trace.Tracer:
    """Get a tracer from the current provider."""
    return trace.get_tracer(name)


def force_flush_telemetry(timeout_millis: int = 5000) -> bool:
    """Flush queued spans, primarily for simulations and orderly shutdown."""
    provider = _provider
    if provider is None:
        return True
    try:
        return bool(provider.force_flush(timeout_millis=timeout_millis))
    except Exception:
        logger.exception("Failed to flush OpenTelemetry spans")
        return False


def reset_telemetry() -> None:
    """Reset the module initialization flag for tests.

    OpenTelemetry does not support replacing the process-global provider after
    it has been installed, so the existing provider is intentionally retained.
    """
    global _initialized
    _initialized = False
