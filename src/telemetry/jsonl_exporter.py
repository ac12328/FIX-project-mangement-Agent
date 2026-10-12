"""Privacy-safe local JSONL OpenTelemetry span exporter."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logger = logging.getLogger(__name__)

_SAFE_METRIC_SUFFIXES = (
    "bytes",
    "chars",
    "count",
    "length",
    "ms",
    "seconds",
    "size",
    "tokens",
)

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "body",
    "content",
    "cookie",
    "email",
    "input",
    "output",
    "password",
    "payload",
    "prompt",
    "response",
    "secret",
    "stacktrace",
)


def _safe_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if lowered in {"exception.message", "error.message"}:
        return "[redacted]"
    leaf = lowered.replace("-", "_").replace(".", "_").split("_")[-1]
    is_safe_metric = leaf in _SAFE_METRIC_SUFFIXES
    if not is_safe_metric and any(part in lowered for part in _SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Mapping):
        return {str(k): _safe_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(key, item) for item in value]
    return str(value)


def _safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        str(key): _safe_value(str(key), value)
        for key, value in (attributes or {}).items()
    }


def span_to_json(span: ReadableSpan) -> dict[str, Any]:
    """Convert a completed span to one stable, privacy-safe JSON object."""
    context = span.get_span_context()
    parent = span.parent
    status_code = getattr(span.status.status_code, "name", str(span.status.status_code))
    duration_ms = None
    if span.start_time is not None and span.end_time is not None:
        duration_ms = round((span.end_time - span.start_time) / 1_000_000, 3)
    scope = getattr(span, "instrumentation_scope", None)
    if scope is None:
        scope = getattr(span, "instrumentation_info", None)
    return {
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "name": span.name,
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
        "parent_span_id": f"{parent.span_id:016x}" if parent is not None else None,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "duration_ms": duration_ms,
        "status": status_code,
        "attributes": _safe_attributes(span.attributes),
        "events": [
            {
                "name": event.name,
                "timestamp_unix_nano": event.timestamp,
                "attributes": _safe_attributes(event.attributes),
            }
            for event in span.events
        ],
        "resource": _safe_attributes(span.resource.attributes),
        "instrumentation_scope": {
            "name": getattr(scope, "name", None),
            "version": getattr(scope, "version", None),
        },
    }


class JsonlSpanExporter(SpanExporter):
    """Append completed spans to a local JSONL file.

    Use this exporter with ``BatchSpanProcessor`` so request threads only enqueue
    completed spans; filesystem I/O is performed by the processor worker.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._shutdown = False

    def export(self, spans: Iterable[ReadableSpan]) -> SpanExportResult:
        if self._shutdown:
            return SpanExportResult.FAILURE
        try:
            rows = [json.dumps(span_to_json(span), ensure_ascii=False, separators=(",", ":")) for span in spans]
            if not rows:
                return SpanExportResult.SUCCESS
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write("\n".join(rows))
                    handle.write("\n")
            return SpanExportResult.SUCCESS
        except Exception:
            logger.exception("Failed to export OpenTelemetry spans to %s", self.path)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._shutdown = True
