"""Emit one LLM request/result event pair without changing inference attribution."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.observability.contracts import TraceContext, trace_context_scope
from src.observability import lifecycle

logger = logging.getLogger(__name__)


def emit_llm_trace_events(
    *,
    context: TraceContext,
    implicit: bool,
    task: str,
    llm_call_id: str,
    metadata: dict[str, Any],
    messages: list[dict[str, Any]] | None,
    request_metadata: dict[str, Any] | None,
    output_text: str | None,
    error: Exception | None,
    latency_ms: int,
    request_sent: bool,
) -> None:
    """Queue paired events; tracing failure must not prevent normal LLM output."""
    request_timestamp = (
        datetime.now(timezone.utc) - timedelta(milliseconds=max(0, latency_ms or 0))
    ).isoformat()
    scope = trace_context_scope(context) if implicit else None
    try:
        if scope is not None:
            scope.__enter__()
            lifecycle.emit_run_start(
                context,
                metadata={"surface": "background", "task": task, "implicit": True},
            )
        lifecycle.emit_event(
            context,
            "llm.request",
            llm_call_id=llm_call_id,
            timestamp=request_timestamp,
            status="running" if request_sent else "skipped",
            metadata={
                **metadata,
                "tokens_in": 0,
                "tokens_out": 0,
                "latency_ms": 0,
            },
            payload={
                "messages": messages or [],
                "provider_request": request_metadata or {},
            },
        )
        lifecycle.emit_event(
            context,
            "llm.error" if error is not None else "llm.response",
            llm_call_id=llm_call_id,
            duration_ms=float(latency_ms or 0),
            status="error" if error is not None else "ok",
            metadata=metadata,
            payload=(
                {
                    "error": str(error),
                    **(
                        {"partial_response": output_text}
                        if output_text is not None
                        else {}
                    ),
                }
                if error is not None
                else {"assembled_response": output_text}
            ),
        )
        if implicit:
            lifecycle.emit_run_finish(
                context,
                status="error" if error is not None else "completed",
                exit_reason="llm_error" if error is not None else "model_stop",
                final_output=output_text if error is None else None,
                persistence_status="not_requested",
                anomaly_flags=[metadata["error_class"]] if error is not None else [],
            )
    except Exception:
        logger.debug("Failed to enqueue LLM engineering trace (task=%s)", task, exc_info=True)
    finally:
        if scope is not None:
            scope.__exit__(None, None, None)
