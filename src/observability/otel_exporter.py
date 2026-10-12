"""OpenTelemetry metadata exporter for engineering lifecycle events."""
from __future__ import annotations

from typing import Any

from opentelemetry import trace


class OpenTelemetryEventExporter:
    """Export safe structured event metadata as short OpenTelemetry spans."""

    name = "opentelemetry"

    def export(self, record: dict[str, Any]) -> None:
        event = record.get("event") or {}
        tracer = trace.get_tracer("agentfixbpro.engineering")
        with tracer.start_as_current_span(f"engineering.{event.get('event_type', 'event')}") as span:
            for key in (
                "event_id", "trace_id", "run_id", "turn_id", "thread_id", "round_index",
                "llm_call_id", "tool_call_id", "status", "component", "component_version",
            ):
                value = event.get(key)
                if value is not None:
                    span.set_attribute(f"engineering.{key}", value)
            duration = event.get("duration_ms")
            if duration is not None:
                span.set_attribute("engineering.duration_ms", float(duration))
            metadata = event.get("metadata") or {}
            for key, value in metadata.items():
                if isinstance(value, (str, bool, int, float)):
                    span.set_attribute(f"engineering.meta.{key}", value)
