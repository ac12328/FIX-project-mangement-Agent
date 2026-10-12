"""Sanitization helpers for the engineering trace explorer API."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

CLASSIC_TRACE_FIELDS = (
    "trace_id", "task", "tokens_in", "tokens_out", "latency_ms",
    "output_valid", "error_class", "decision_outcome", "prompt_hash",
    "run_id", "created_at",
)
RUN_FIELDS = (
    "run_id", "surface", "task", "project_id", "status", "exit_reason",
    "started_at", "ended_at", "duration_ms", "implicit",
)
RUN_EXPORT_FIELDS = (
    "schema_version", "trace_id", "turn_id", "thread_id", *RUN_FIELDS,
    "component", "component_version", "persistence_status",
)
EVENT_FIELDS = (
    "schema_version", "event_id", "trace_id", "run_id", "turn_id",
    "thread_id", "sequence", "event_type", "round_index", "llm_trace_id",
    "llm_call_id", "tool_call_id", "tool_name", "status", "duration_ms",
    "component", "component_version", "parent_event_id", "parent_span_id",
    "payload_available", "created_at",
)
DIAGNOSTIC_FIELDS = (
    "diagnostic_id", "run_id", "trigger", "status", "severity",
    "root_cause", "summary", "created_at", "completed_at",
)
LLM_EXPORT_FIELDS = (
    *CLASSIC_TRACE_FIELDS, "llm_call_id", "turn_id", "round_index", "ttft_ms",
    "retry_count", "failover_from", "cache_hit", "cache_read_tokens",
    "cache_write_tokens", "cache_capability_status", "cache_reporting_status",
    "request_event_id", "response_event_id",
)
FEEDBACK_FIELDS = (
    "feedback_id", "item_type", "item_id", "project_id", "feedback_kind",
    "category", "surface", "trace_id", "run_id", "turn_id",
    "output_revision_id", "created_at",
)
TRACE_HEALTH_FIELDS = (
    "enabled", "started", "worker_alive", "queue_size", "queue_capacity",
    "pending_events", "outstanding_events", "spool_lag_ms",
    "emit_sample_count", "emit_p50_ms", "emit_p95_ms", "emit_p99_ms", "emit_max_ms",
    "queue_lag_sample_count", "queue_lag_p50_ms", "queue_lag_p95_ms",
    "queue_lag_p99_ms", "queue_lag_max_ms", "enqueued", "projected", "queue_drops",
    "spool_errors", "encryption_errors", "projection_errors",
    "projection_recoveries", "projection_failure_active",
    "projection_consecutive_errors", "last_projection_error_at",
    "last_projection_error_class", "last_projection_success_at",
    "exporter_errors", "recovered", "duplicates", "exporters",
)
SESSION_HEALTH_FIELDS = (
    "running", "queue_depth", "pending_sessions", "queue_lag_ms", "enqueued",
    "processed", "dropped", "errors", "last_error",
)
TOOL_CENTRIC_SHADOW_HEALTH_FIELDS = (
    "enabled", "running", "worker_count", "queue_depth", "queue_capacity",
    "queue_lag_ms", "max_queue_lag_ms", "enqueued", "processed", "completed",
    "fallback", "failed", "dropped", "errors", "last_error_class",
    "planner_timeout_seconds",
)
EMAIL_PROJECT_TRUTH_SHADOW_HEALTH_FIELDS = (
    "mode", "enabled", "serving_enabled", "running", "worker_count", "queue_depth", "queue_capacity",
    "queue_lag_ms", "max_queue_lag_ms", "enqueued", "processed", "completed",
    "failed", "dropped", "errors", "persistence_errors", "last_error_class",
    "timeout_seconds",
)


def select_fields(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row.get(field) for field in fields}


def sanitize_classic_trace(row: Mapping[str, Any]) -> dict[str, Any]:
    return select_fields(row, CLASSIC_TRACE_FIELDS)


def sanitize_run(row: Mapping[str, Any]) -> dict[str, Any]:
    item = select_fields(row, RUN_FIELDS)
    item["implicit"] = bool(item["implicit"])
    return item


def sanitize_export_run(row: Mapping[str, Any]) -> dict[str, Any]:
    item = select_fields(row, RUN_EXPORT_FIELDS)
    item["implicit"] = bool(item["implicit"])
    return item


def sanitize_event(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["payload_available"] = bool(
        item.get("payload_available") or item.get("payload_ciphertext")
    )
    return select_fields(item, EVENT_FIELDS)


def sanitize_diagnostic(row: Mapping[str, Any]) -> dict[str, Any]:
    return select_fields(row, DIAGNOSTIC_FIELDS)


def sanitize_feedback(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return correlation metadata without operator-authored plaintext notes."""
    return select_fields(row, FEEDBACK_FIELDS)


def sanitize_llm_export(row: Mapping[str, Any]) -> dict[str, Any]:
    item = select_fields(row, LLM_EXPORT_FIELDS)
    trace_metadata = row.get("trace_metadata")
    if isinstance(trace_metadata, str):
        try:
            trace_metadata = json.loads(trace_metadata)
        except (TypeError, ValueError):
            trace_metadata = {}
    if not isinstance(trace_metadata, Mapping):
        trace_metadata = {}
    for field in ("cache_capability_status", "cache_reporting_status"):
        item[field] = item.get(field) or trace_metadata.get(field)
    if item.get("cache_hit") is not None:
        item["cache_hit"] = bool(item["cache_hit"])
    return item


def event_payload_envelope(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if not row.get("payload_ciphertext"):
        return None
    return {
        "ciphertext": row.get("payload_ciphertext"),
        "nonce": row.get("payload_nonce"),
        "key_version": row.get("payload_key_version"),
        "payload_hash": row.get("payload_hash"),
    }
