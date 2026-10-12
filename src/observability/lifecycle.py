"""Helpers for emitting engineering lifecycle events without blocking callers."""
from __future__ import annotations

import hashlib
import json
import platform
import time
from functools import lru_cache
from typing import Any

from opentelemetry import trace

from src.observability.contracts import (
    TRACE_SCHEMA_VERSION,
    EngineeringTraceEvent,
    TraceContext,
    current_round_index,
)
from src.observability.pipeline import emit_trace_event


def current_span_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return f"{span_context.span_id:016x}"


def emit_event(context: TraceContext, event_type: str, **kwargs: Any) -> EngineeringTraceEvent:
    kwargs.setdefault("parent_span_id", current_span_id())
    active_round = current_round_index()
    if active_round is not None:
        kwargs.setdefault("round_index", active_round)
    event = context.make_event(event_type, **kwargs)
    emit_trace_event(event)
    return event


@lru_cache(maxsize=1)
def _config_schema_hash() -> str:
    """Return a credential-free fingerprint of the validated config contract."""
    try:
        from config.settings import AppConfig

        schema = AppConfig.model_json_schema()
    except Exception:
        schema = {"contract": "agentfixbpro-app-config-v1"}
    return content_hash(schema)


def run_version_metadata(
    context: TraceContext,
    *,
    preparation_version: str | None = None,
) -> dict[str, Any]:
    """Return privacy-safe runtime/config/prompt/tool version identifiers.

    Exact invocation prompt/tool hashes are emitted later in ``context.composed``
    and ``prepare.snapshot`` once selection is complete. These stable identifiers
    make every ``run.start`` self-describing without reading secrets or delaying
    the request path.
    """
    return {
        "runtime_version": context.component_version,
        "python_version": platform.python_version(),
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "config_version": "app-config-schema-v1",
        "config_schema_hash": _config_schema_hash(),
        "prompt_version": preparation_version or "context-composition-v1",
        "tool_version": "openai-tool-contract-v1",
        "invocation_hashes_event": "prepare.snapshot",
    }


def tool_contract_metadata(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a safe exact fingerprint for the selected tool contracts."""
    normalized = json.dumps(
        tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return {
        "tool_count": len(tools),
        "tool_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "tool_version": "openai-tool-contract-v1",
    }


def emit_run_start(
    context: TraceContext,
    *,
    metadata: dict[str, Any] | None = None,
    payload: Any = None,
) -> EngineeringTraceEvent | None:
    if not context.mark_started():
        return None
    supplied = metadata or {}
    values = {
        "surface": context.surface,
        "task": context.task,
        "implicit": context.implicit,
        **run_version_metadata(
            context, preparation_version=supplied.get("preparation_version")
        ),
        **supplied,
    }
    event = emit_event(context, "run.start", status="running", metadata=values, payload=payload)
    context.parent_event_id = event.event_id
    return event


def emit_run_finish(
    context: TraceContext,
    *,
    status: str,
    exit_reason: str,
    final_output: Any = None,
    persistence_status: str | None = None,
    user_feedback: Any = None,
    anomaly_flags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> EngineeringTraceEvent | None:
    if not context.mark_finished():
        return None
    duration_ms = (time.monotonic() - context.started_monotonic) * 1000
    values = {
        "exit_reason": exit_reason,
        "persistence_status": persistence_status,
        "user_feedback_status": (
            "captured" if user_feedback is not None else "not_available_at_finish"
        ),
        "anomaly_flags": anomaly_flags or [],
        **(metadata or {}),
    }
    payload: dict[str, Any] = {}
    if final_output is not None:
        values["final_output_hash"] = content_hash(final_output)
        payload["final_output"] = final_output
    if user_feedback is not None:
        values["user_feedback_hash"] = content_hash(user_feedback)
        payload["user_feedback"] = user_feedback
    return emit_event(
        context,
        "run.finish",
        status=status,
        duration_ms=duration_ms,
        metadata=values,
        payload=payload or None,
    )


def content_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def context_composition_metadata(
    messages: list[dict[str, Any]],
    *,
    profile: str | None,
    context_frame: Any = None,
) -> dict[str, Any]:
    stable: list[dict[str, Any]] = []
    dynamic: list[dict[str, Any]] = []
    seen_non_system = False
    for message in messages:
        if not seen_non_system and message.get("role") == "system":
            stable.append(message)
        else:
            seen_non_system = True
            dynamic.append(message)
    sources: list[str] = []
    if context_frame is not None:
        for name in (
            "identity", "project_truth", "relationship", "relationship_truth", "evidence",
            "knowledge", "operational_memory", "learned_memory", "memory", "conversation", "instructions",
        ):
            if getattr(context_frame, name, None):
                sources.append(name)
    try:
        from src.llm.client import _estimate_tokens

        estimated_tokens = _estimate_tokens(messages)
    except Exception:
        estimated_tokens = int(len(json.dumps(messages, ensure_ascii=False, default=str)) / 3.5)
    return {
        "context_profile": profile,
        "message_count": len(messages),
        "estimated_tokens": estimated_tokens,
        "context_bytes": len(json.dumps(messages, ensure_ascii=False, default=str).encode("utf-8")),
        "source_names": sources,
        "stable_prefix_hash": content_hash(stable),
        "dynamic_prefix_hash": content_hash(dynamic),
    }
