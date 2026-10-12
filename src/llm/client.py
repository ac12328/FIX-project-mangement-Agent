"""
LLM Client ??all LLM calls go through here.
Uses LiteLLM for unified API across OpenAI / Anthropic / Google.
Optionally records traces to llm_traces table for observability.
"""

import hashlib
import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from src.llm import litellm_transport as litellm
from opentelemetry import trace

from config.defaults import MODEL_CONTEXT_WINDOW
from src.llm.failover import should_failover_to_backup
from src.llm.trace_emission import emit_llm_trace_events
from src.llm.request_construction import (
    add_json_mode,
    add_structured_tool,
    build_backup_kwargs,
    build_completion_kwargs,
    build_stream_kwargs,
)
from src.llm.trace_metadata import (
    _metadata_value,
    _metadata_field,
    _usage_cache_metadata,
    _response_cache_metadata,
    _response_retry_count,
    _response_trace_metadata,
)
from src.llm.request_budget import (
    content_char_cost as _budget_content_char_cost,
    prompt_char_count as _budget_prompt_char_count,
    estimate_tokens as _budget_estimate_tokens,
    cap_max_tokens as _budget_cap_max_tokens,
    safe_truncate_content as _budget_safe_truncate_content,
    trim_messages_to_fit as _budget_trim_messages_to_fit,
)
from src.llm.structured_output import (
    LLMParseFailure,
    _augment_messages_with_schema,
    _grok_46_schema_contract,
    _infer_schema_property,
    _normalize_response_schema,
    _resolve_local_schema_ref,
    _schema_type_label,
    _uses_grok_46_schema_prompt,
    _parse_json_response as _parse_structured_json,
    _structured_tool_message,
)
from src.llm.routing import ModelConfig, get_model_for_task
from src.llm.watchdog import LLMTimeoutError, LLM_CALL_TIMEOUT_SEC, call_with_timeout, push_on_error  # noqa: F401
from src.llm import status_tracker
from src.observability.traces import hash_prompt

logger = logging.getLogger(__name__)

# Module-level tracing flag ??set via enable_tracing()
_tracing_enabled = False
_trace_ids_var: ContextVar[tuple[str, ...]] = ContextVar("llm_trace_ids", default=())

# Active learned rule IDs for trace metadata. Scoped per async/thread context
# (like _trace_ids_var) so concurrent LLM calls do not overwrite each other's
# rule set and contaminate trace metadata.
_active_rule_ids_var: ContextVar[tuple[str, ...]] = ContextVar(
    "llm_active_rule_ids", default=()
)

_MIN_OUTPUT_TOKENS = 512
_CONTEXT_MARGIN = 300
_last_completed_model_var: ContextVar[str | None] = ContextVar("llm_last_completed_model", default=None)


@dataclass(frozen=True)
class InferenceLink:
    """Immutable identity of the exact persisted inference output."""

    trace_id: str
    output_revision_id: str


@dataclass(frozen=True)
class StructuredLLMResponse:
    """Structured data plus the exact persisted inference identity."""

    data: dict[str, Any]
    model: str
    inference: InferenceLink | None = None


_last_inference_link_var: ContextVar[InferenceLink | None] = ContextVar(
    "llm_last_inference_link", default=None,
)


def get_last_completed_model() -> str | None:
    return _last_completed_model_var.get()


def _content_char_cost(content: Any) -> int:
    """Keep the legacy sizing entry point for callers and patch seams."""
    return _budget_content_char_cost(content)


def _prompt_char_count(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    return _budget_prompt_char_count(messages, tools, content_cost=_content_char_cost)


def _estimate_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    return _budget_estimate_tokens(messages, tools, char_count=_prompt_char_count, margin=_CONTEXT_MARGIN)


def _set_llm_request_span_attributes(
    span: Any,
    *,
    task: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int | None,
    timeout_seconds: int | float,
    temperature: float | None,
    streaming: bool,
    attempt: str = "primary",
    attempt_number: int = 1,
    max_retries: int = 0,
    fallback_count: int = 0,
    model_override: bool = False,
    failover_trigger: str | None = None,
    structured_mode: str | None = None,
) -> None:
    """Attach privacy-safe request sizing and routing metadata to an LLM span."""
    span.set_attribute("llm.task", task)
    span.set_attribute("llm.model", model)
    span.set_attribute("llm.prompt_chars", _prompt_char_count(messages, tools))
    span.set_attribute("llm.estimated_input_tokens", _estimate_tokens(messages, tools))
    span.set_attribute("llm.timeout_seconds", float(timeout_seconds))
    span.set_attribute("llm.streaming", streaming)
    span.set_attribute("llm.attempt", attempt)
    span.set_attribute("llm.attempt_number", attempt_number)
    span.set_attribute("llm.retry.max_retries", max(0, int(max_retries or 0)))
    span.set_attribute("llm.failover.configured", fallback_count > 0)
    span.set_attribute("llm.failover.fallback_count", max(0, int(fallback_count)))
    span.set_attribute("llm.model_override", model_override)
    span.set_attribute("llm.num_tools", len(tools or ()))
    if max_tokens is not None:
        span.set_attribute("llm.max_tokens", int(max_tokens))
    if temperature is not None:
        span.set_attribute("llm.temperature", float(temperature))
    if failover_trigger:
        span.set_attribute("llm.failover.trigger", failover_trigger)
    if structured_mode:
        span.set_attribute("llm.structured_mode", structured_mode)


def _set_llm_response_span_attributes(
    span: Any,
    *,
    latency_ms: int | float,
    tokens_in: int = 0,
    tokens_out: int = 0,
    output_chars: int = 0,
    response_model: str | None = None,
    tool_call_count: int | None = None,
    first_chunk_ms: int | float | None = None,
    ttft_ms: int | float | None = None,
) -> None:
    """Attach privacy-safe response metrics to a completed LLM span."""
    span.set_attribute("llm.latency_ms", round(float(latency_ms), 3))
    span.set_attribute("llm.tokens_in", max(0, int(tokens_in or 0)))
    span.set_attribute("llm.tokens_out", max(0, int(tokens_out or 0)))
    span.set_attribute("llm.output_chars", max(0, int(output_chars or 0)))
    if response_model:
        span.set_attribute("llm.response_model", str(response_model))
    if tool_call_count is not None:
        span.set_attribute("llm.tool_calls", max(0, int(tool_call_count)))
    if first_chunk_ms is not None:
        rounded_first_chunk = round(float(first_chunk_ms), 3)
        span.set_attribute("llm.first_chunk_ms", rounded_first_chunk)
        span.add_event("first_chunk", {"elapsed_ms": rounded_first_chunk})
    if ttft_ms is not None:
        rounded_ttft = round(float(ttft_ms), 3)
        span.set_attribute("llm.ttft_ms", rounded_ttft)
        span.add_event("first_visible_token", {"elapsed_ms": rounded_ttft})


def _cap_max_tokens(
    requested: int | None,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> int | None:
    """Keep the configured window and facade estimate patchable at call time."""
    return _budget_cap_max_tokens(
        requested, messages, tools,
        context_window=MODEL_CONTEXT_WINDOW,
        min_output_tokens=_MIN_OUTPUT_TOKENS,
        estimate=_estimate_tokens,
        log=logger,
    )


def _safe_truncate_content(content: str, max_chars: int) -> str:
    return _budget_safe_truncate_content(content, max_chars)


def _trim_messages_to_fit(
    messages: list[dict],
    tools: list[dict] | None = None,
    reserve_output: int = _MIN_OUTPUT_TOKENS,
) -> list[dict]:
    """Preserve call-time sizing hooks; do not mutate inputs or return over budget."""
    return _budget_trim_messages_to_fit(
        messages, tools, reserve_output,
        context_window=MODEL_CONTEXT_WINDOW,
        estimate=_estimate_tokens,
        content_cost=_content_char_cost,
        truncate=_safe_truncate_content,
        log=logger,
    )


def set_active_rules(rule_ids: list[str]) -> None:
    """Set the currently active learned rule IDs for trace recording.

    Scoped per async/thread context so concurrent LLM calls do not overwrite
    each other's rule set.
    """
    _active_rule_ids_var.set(tuple(rule_ids))


def get_active_rules() -> list[str]:
    """Return the currently active learned rule IDs."""
    return list(_active_rule_ids_var.get())


def strip_reasoning(raw: str) -> str:
    """Strip LLM reasoning tags without corrupting JSON content."""
    raw = raw.strip()
    json_start = len(raw)
    for ch in "{[":
        pos = raw.find(ch)
        if 0 <= pos < json_start:
            json_start = pos
    think_end_pos = raw.find("</think>")
    if 0 <= think_end_pos < json_start:
        raw = raw[think_end_pos + len("</think>"):].lstrip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    return raw


def enable_tracing(conn=None) -> None:
    """Enable LLM call tracing. Connection is obtained per-thread via scoped_write()."""
    global _tracing_enabled
    _tracing_enabled = True


def reset_trace_context() -> None:
    """Clear per-request trace and exact exposure linkage context."""
    _trace_ids_var.set(())
    try:
        from src.memory.exposure_context import clear_exposure_scope
        clear_exposure_scope()
    except ImportError:
        pass


def get_trace_ids() -> list[str]:
    """Return trace IDs recorded in the current execution context."""
    return list(_trace_ids_var.get(()))


def _record_trace(
    task: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    error: Exception | None = None,
    prompt_hash: str | None = None,
    per_guard_results: list[dict] | None = None,
    *,
    output_text: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    ttft_ms: int | float | None = None,
    retry_count: int = 0,
    failover_from: str | None = None,
    cache_hit: bool | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_capability_status: str = "unknown",
    cache_reporting_status: str = "not_reported",
    decision_outcome: str | None = None,
    request_metadata: dict[str, Any] | None = None,
    attempt: str = "primary",
    attempt_number: int = 1,
    failover_trigger: str | None = None,
    streaming: bool = False,
    request_sent: bool = True,
) -> InferenceLink | None:
    """Emit asynchronous engineering LLM events and exact attribution linkage."""
    if not _tracing_enabled:
        return None

    from src.observability.contracts import (
        TraceContext,
        current_round_index,
        current_trace_context,
        new_identifier,
    )
    from src.observability.traces import classify_error

    context = current_trace_context()
    implicit = context is None
    if context is None:
        context = TraceContext.create(
            surface="background",
            task=task,
            implicit=True,
        )
    llm_call_id = new_identifier("tr")
    active_rules = _active_rule_ids_var.get()
    metadata: dict[str, Any] = {
        "task": task,
        "model": model,
        "tokens_in": int(tokens_in or 0),
        "tokens_out": int(tokens_out or 0),
        "latency_ms": int(latency_ms or 0),
        "prompt_hash": prompt_hash,
        "guard_results": per_guard_results or [],
        "active_rules": list(active_rules),
        "ttft_ms": ttft_ms,
        "retry_count": int(retry_count or 0),
        "failover_from": failover_from,
        "cache_hit": cache_hit,
        "cache_read_tokens": int(cache_read_tokens or 0),
        "cache_write_tokens": int(cache_write_tokens or 0),
        "cache_capability_status": cache_capability_status,
        "cache_reporting_status": cache_reporting_status,
        "decision_outcome": decision_outcome,
        "attempt": attempt,
        "attempt_number": max(1, int(attempt_number or 1)),
        "failover_trigger": failover_trigger,
        "streaming": bool(streaming),
        "provider_request_sent": bool(request_sent),
        "round_index": current_round_index(),
    }
    try:
        from src.observability.context_budget import trace_metadata as _budget_meta

        budget = _budget_meta()
        if budget:
            metadata.update(budget)
    except Exception:
        pass
    if error is not None:
        err_msg = str(error).strip() or type(error).__name__
        metadata["error_class"] = classify_error(error)
        metadata["error_message"] = err_msg[:2000]

    emit_llm_trace_events(
        context=context,
        implicit=implicit,
        task=task,
        llm_call_id=llm_call_id,
        metadata=metadata,
        messages=messages,
        request_metadata=request_metadata,
        output_text=output_text,
        error=error,
        latency_ms=latency_ms,
        request_sent=request_sent,
    )

    _trace_ids_var.set((*_trace_ids_var.get(()), llm_call_id))
    revision_id = _record_exact_inference_attribution(
        trace_id=llm_call_id,
        task=task,
        output_text=output_text or "",
        messages=messages or [],
    )
    link = InferenceLink(llm_call_id, revision_id) if revision_id else None
    _last_inference_link_var.set(link)
    return link


def _record_backup_configuration_error(
    *,
    task: str,
    backup: ModelConfig,
    messages: list[dict[str, Any]],
    prompt_hash: str | None,
    failover_trigger: str,
    streaming: bool,
    tool_mode: bool = False,
    attempt_number: int = 2,
    skip_reason: str = "backup_endpoint_not_configured",
) -> None:
    """Record a truthful skipped backup attempt when failover is unavailable."""
    error_message = {
        "backup_endpoint_not_configured": "Backup endpoint not configured",
        "backup_model_unavailable": "Backup model unavailable",
    }.get(skip_reason, f"Backup unavailable: {skip_reason}")
    error = RuntimeError(error_message)
    _record_trace(
        task,
        f"backup:{backup.primary}",
        0,
        0,
        0,
        error=error,
        prompt_hash=prompt_hash,
        messages=messages,
        failover_from=get_model_for_task(task).primary,
        attempt="backup",
        attempt_number=attempt_number,
        failover_trigger=failover_trigger,
        streaming=streaming,
        decision_outcome="configuration_error",
        request_metadata={
            "model": backup.primary,
            "api_base_configured": bool(backup.api_base),
            "stream": streaming,
            "tool_mode": tool_mode,
            "skip_reason": skip_reason,
        },
        request_sent=False,
    )


def _dedupe_attached_exposures(scopes, prompt: str):
    """Select one deterministic, most-specific current scope per fact revision.

    Ordering: project-scoped beats unscoped; then later/nested scope beats earlier;
    remaining ties use exact provenance fields for stable cross-run ordering.
    """
    selected = {}
    for index, scope in enumerate(scopes):
        for item in scope.candidates:
            if not item.rendered_text or item.rendered_text not in prompt:
                continue
            key = (item.fact_id, item.fact_version)
            priority = (
                int(scope.project_id is not None), index,
                scope.surface, scope.project_id or "", scope.task_type or "",
            )
            current = selected.get(key)
            if current is None or priority > current[0]:
                selected[key] = (priority, scope, item)
    return [
        (scope, item) for _, scope, item in sorted(
            selected.values(), key=lambda value: (
                value[2].rank, value[2].fact_id, value[2].fact_version,
                value[1].surface, value[1].project_id or "",
            ),
        )
    ]


def _record_exact_inference_attribution(
    *, trace_id: str, task: str, output_text: str,
    messages: list[dict[str, Any]],
) -> str | None:
    """Persist functional learning lineage only for an explicit inference scope.

    Engineering trace emission is complete before this function is called and
    never opens SQLite. Exact attribution is a separate learning/draft-lineage
    contract that must return an ``InferenceLink`` synchronously to its caller.
    Keep its legacy write bounded and fail-open, but avoid acquiring the writer
    entirely for the normal Agent/LLM path where no inference scope is active.
    """
    from src.learning.runtime import learning_writes_disabled
    from src.memory.exposure_context import get_exposure_scopes, get_inference_scope

    if learning_writes_disabled():
        return None
    primary = get_inference_scope()
    if primary is None:
        return None
    scopes = get_exposure_scopes()
    try:
        from src.db.connection import scoped_observability_write

        with scoped_observability_write(timeout=0.05) as conn:
            return _record_exact_memory_exposures(
                conn,
                trace_id=trace_id,
                task=task,
                output_text=output_text,
                messages=messages,
                primary=primary,
                scopes=scopes,
            )
    except Exception:
        logger.debug(
            "Failed to record exact inference attribution (task=%s)",
            task,
            exc_info=True,
        )
        return None


def _record_exact_memory_exposures(
    conn, *, trace_id: str, task: str, output_text: str,
    messages: list[dict[str, Any]], primary=None, scopes=None,
) -> str | None:
    """Persist only request-local facts demonstrably attached to this prompt."""
    from src.memory.exposure_context import get_exposure_scopes, get_inference_scope
    from src.memory.exposure_repo import MemoryExposure, insert_exposures
    from src.memory.output_revision_repo import create_output_revision

    if primary is None:
        primary = get_inference_scope()
    if scopes is None:
        scopes = get_exposure_scopes()
    if not primary:
        return None
    prompt = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    attached = _dedupe_attached_exposures(scopes, prompt)
    revision_id = primary.output_revision_id or (
        "or_" + hashlib.sha256(
            f"{trace_id}|{primary.surface}|{output_text}".encode("utf-8")
        ).hexdigest()[:24]
    )
    create_output_revision(
        conn, output_revision_id=revision_id, operator_id=primary.operator_id,
        surface=primary.surface, artifact_type="inference", artifact_id=trace_id,
        revision=1, trace_id=trace_id, content=output_text,
    )
    insert_exposures(conn, [MemoryExposure(
        trace_id=trace_id, output_revision_id=revision_id,
        operator_id=primary.operator_id, surface=primary.surface,
        source_surface=scope.surface, task_type=scope.task_type or task,
        project_id=scope.project_id, **item.model_dump(),
    ) for scope, item in attached if scope.operator_id == primary.operator_id])
    return revision_id


def llm_call(
    task: str,
    messages: list[dict[str, Any]],
    *,
    model_override: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_sec: int | None = None,
    notify_on_error: bool = True,
    _trace_attempt: str = "primary",
    _trace_attempt_number: int = 1,
    _trace_failover_trigger: str | None = None,
    _trace_decision_outcome: str | None = None,
) -> str:
    """
    Make a simple LLM call and return the text response.

    Args:
        task: Task type key for model routing (e.g. "triage", "draft").
        messages: OpenAI-format messages list.
        model_override: Force a specific model instead of task routing.
        temperature: Override default temperature.
        max_tokens: Override default max_tokens.
        notify_on_error: Whether to publish a user-facing error card when the call fails.

    Returns:
        The assistant's text response.
    """
    config = get_model_for_task(task)
    model = model_override or config.primary
    temp = temperature if temperature is not None else config.temperature
    tokens = max_tokens if max_tokens is not None else config.max_tokens
    messages = _trim_messages_to_fit(messages)
    tokens = _cap_max_tokens(tokens, messages)
    prompt_hash = hash_prompt(json.dumps(messages, ensure_ascii=False))
    _timeout = timeout_sec if timeout_sec is not None else LLM_CALL_TIMEOUT_SEC

    if not model_override and status_tracker.is_circuit_open("primary"):
        logger.info("llm_call circuit open ??skipping primary, using backup directly (task=%s)", task)
        backup_result = _try_backup(
            task, messages, temp, tokens, _timeout, prompt_hash,
            failover_trigger="circuit_open",
            attempt_number=max(2, _trace_attempt_number + 1),
        )
        if backup_result is not None:
            return backup_result
        # Backup also failed while the primary circuit is open. Do NOT fall
        # through to the try block ??that would call the known-bad primary
        # again, wasting a full timeout cycle and re-confirming the
        # failure instead of surfacing it to the caller.
        raise RuntimeError(f"Primary circuit open and backup failed (task={task})")

    try:
        tracer = trace.get_tracer("agentfixbpro")
        with tracer.start_as_current_span("llm.call") as span:
            _set_llm_request_span_attributes(
                span,
                task=task,
                model=model,
                messages=messages,
                max_tokens=tokens,
                timeout_seconds=_timeout,
                temperature=temp,
                streaming=False,
                attempt=_trace_attempt,
                attempt_number=_trace_attempt_number,
                failover_trigger=_trace_failover_trigger,
                max_retries=config.max_retries,
                fallback_count=len(config.fallbacks or ()),
                model_override=model_override is not None,
            )
            t0 = time.monotonic()
            call_kwargs = _build_completion_kwargs(
                config=config, model=model, messages=messages,
                temperature=temp, max_tokens=tokens, model_override=model_override,
            )
            msg_str = json.dumps(call_kwargs.get("messages", []), indent=2)
            logger.debug("LLM CALL [%s] | chars: %d | messages:\n%s", task, len(msg_str), msg_str)
            response = call_with_timeout(lambda: litellm.completion(**call_kwargs), _timeout)
            latency = int((time.monotonic() - t0) * 1000)
            content = response.choices[0].message.content or ""
            tokens_in = response.usage.prompt_tokens
            tokens_out = response.usage.completion_tokens
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(content),
                response_model=str(response.model),
            )
            logger.info(
                "llm_call task=%s model=%s tokens_in=%d tokens_out=%d",
                task, response.model, tokens_in, tokens_out,
            )
            _record_trace(
                task,
                response.model,
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=prompt_hash,
                output_text=content,
                messages=messages,
                request_metadata=call_kwargs,
                attempt=_trace_attempt,
                attempt_number=_trace_attempt_number,
                failover_trigger=_trace_failover_trigger,
                decision_outcome=_trace_decision_outcome,
                **_response_trace_metadata(response, requested_model=model),
            )
            status_tracker.record_success("primary", latency)
            _last_completed_model_var.set(str(response.model))
            return content

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000) if 't0' in locals() else 0
        logger.error("llm_call failed task=%s model=%s error=%s", task, model, e)
        _record_trace(
            task,
            model,
            0,
            0,
            latency,
            error=e,
            prompt_hash=prompt_hash,
            messages=messages,
            request_metadata=locals().get("call_kwargs"),
            retry_count=_response_retry_count(e),
            attempt=_trace_attempt,
            attempt_number=_trace_attempt_number,
            failover_trigger=_trace_failover_trigger,
            decision_outcome=_trace_decision_outcome,
        )

        if should_failover_to_backup(e) and not model_override:
            status_tracker.record_failure("primary", str(e))
            backup_result = _try_backup(
                task, messages, temp, tokens, _timeout, prompt_hash,
                failover_trigger="primary_error",
                attempt_number=max(2, _trace_attempt_number + 1),
            )
            if backup_result is not None:
                return backup_result

        if notify_on_error:
            push_on_error(task, e, get_trace_ids()[-1] if get_trace_ids() else None)
        raise


def _convert_system_to_user(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert system messages to user messages for providers that don't support the system role.
    Merges consecutive user messages to avoid API rejection."""
    converted: list[dict[str, Any]] = []
    for msg in messages:
        role = "user" if msg.get("role") == "system" else msg.get("role", "user")
        content = msg.get("content", "")
        if converted and converted[-1]["role"] == role == "user":
            converted[-1]["content"] = converted[-1]["content"] + "\n\n" + content
        else:
            converted.append({"role": role, "content": content})
    return converted


def _backup_unavailable_reason(backup: ModelConfig) -> str | None:
    from src.llm.backup_availability import get_backup_unavailable_reason

    return get_backup_unavailable_reason(backup)


def _quarantine_invalid_backup_model(backup: ModelConfig, error: Exception) -> None:
    from src.llm.backup_availability import mark_backup_unavailable_from_error

    if mark_backup_unavailable_from_error(backup, error):
        logger.error(
            "Backup model quarantined for this process model=%s reason=backup_model_unavailable",
            backup.primary,
        )


def _try_backup(
    task: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    timeout_sec: int,
    prompt_hash: str | None,
    *,
    failover_trigger: str = "primary_error",
    attempt_number: int = 2,
) -> str | None:
    """Attempt the call on the local Gemma backup. Returns None on failure."""
    from src.llm.routing import get_backup_config

    backup = get_backup_config()
    if not backup.api_base:
        logger.error(
            "llm_call backup skipped task=%s ? backup_api_base / local_llm_api_base not configured",
            task,
        )
        _record_backup_configuration_error(
            task=task,
            backup=backup,
            messages=messages,
            prompt_hash=prompt_hash,
            failover_trigger=failover_trigger,
            streaming=False,
            attempt_number=attempt_number,
        )
        status_tracker.record_failure("backup", "backup endpoint not configured")
        return None
    unavailable_reason = _backup_unavailable_reason(backup)
    if unavailable_reason:
        logger.error(
            "llm_call backup skipped task=%s model=%s reason=%s",
            task, backup.primary, unavailable_reason,
        )
        _record_backup_configuration_error(
            task=task, backup=backup, messages=messages, prompt_hash=prompt_hash,
            failover_trigger=failover_trigger, streaming=False,
            attempt_number=attempt_number, skip_reason=unavailable_reason,
        )
        return None
    logger.warning(
        "llm_call primary failed ? falling back to backup (%s @ %s)",
        backup.primary,
        backup.api_base,
    )

    try:
        tracer = trace.get_tracer("agentfixbpro")
        with tracer.start_as_current_span("llm.call.backup") as span:
            converted_messages = _convert_system_to_user(messages)
            effective_max_tokens = max_tokens or backup.max_tokens
            _set_llm_request_span_attributes(
                span,
                task=task,
                model=backup.primary,
                messages=converted_messages,
                max_tokens=effective_max_tokens,
                timeout_seconds=timeout_sec,
                temperature=temperature,
                streaming=False,
                attempt="backup",
                attempt_number=attempt_number,
                max_retries=backup.max_retries,
                fallback_count=0,
                failover_trigger=failover_trigger,
            )
            t0 = time.monotonic()
            call_kwargs = build_backup_kwargs(
                config=backup, messages=converted_messages,
                temperature=temperature, max_tokens=effective_max_tokens,
            )
            response = call_with_timeout(
                lambda: litellm.completion(**call_kwargs), timeout_sec
            )
            latency = int((time.monotonic() - t0) * 1000)
            content = response.choices[0].message.content or ""
            tokens_in = response.usage.prompt_tokens
            tokens_out = response.usage.completion_tokens
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(content),
                response_model=str(response.model),
            )
            logger.info(
                "llm_call BACKUP task=%s model=%s tokens_in=%d tokens_out=%d",
                task,
                response.model,
                tokens_in,
                tokens_out,
            )
            _record_trace(
                task,
                f"backup:{response.model}",
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=prompt_hash,
                output_text=content,
                messages=converted_messages,
                request_metadata=call_kwargs,
                attempt="backup",
                attempt_number=attempt_number,
                failover_trigger=failover_trigger,
                **_response_trace_metadata(
                    response,
                    failover_from=get_model_for_task(task).primary,
                ),
            )
            status_tracker.record_success("backup", latency)
            _last_completed_model_var.set(f"backup:{response.model}")
            return content
    except Exception as backup_err:
        latency = int((time.monotonic() - t0) * 1000) if "t0" in locals() else 0
        logger.error("llm_call backup also failed task=%s error=%s", task, backup_err)
        _quarantine_invalid_backup_model(backup, backup_err)
        _record_trace(
            task,
            f"backup:{backup.primary}",
            0,
            0,
            latency,
            error=backup_err,
            prompt_hash=prompt_hash,
            messages=locals().get("converted_messages", messages),
            request_metadata=locals().get("call_kwargs"),
            retry_count=_response_retry_count(backup_err),
            failover_from=get_model_for_task(task).primary,
            attempt="backup",
            attempt_number=attempt_number,
            failover_trigger=failover_trigger,
        )
        status_tracker.record_failure("backup", str(backup_err))
        return None


def llm_call_stream(
    task: str,
    messages: list[dict[str, Any]],
    *,
    on_delta: "Callable[[str], None] | None" = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_sec: int | None = None,
) -> str:
    """Streaming variant of :func:`llm_call`.

    Iterates LiteLLM streaming chunks, fires *on_delta* with each text
    delta, and returns the full accumulated text.  Does NOT use the
    watchdog ``call_with_timeout`` ??timeout is delegated to the HTTP
    client via ``timeout`` in call_kwargs.
    """

    config = get_model_for_task(task)
    model = config.primary
    temp = temperature if temperature is not None else config.temperature
    tokens = max_tokens if max_tokens is not None else config.max_tokens
    messages = _trim_messages_to_fit(messages)
    tokens = _cap_max_tokens(tokens, messages)
    _timeout = timeout_sec if timeout_sec is not None else LLM_CALL_TIMEOUT_SEC
    prompt_hash = hash_prompt(json.dumps(messages, ensure_ascii=False))

    if status_tracker.is_circuit_open("primary"):
        logger.info("llm_call_stream circuit open ??skipping primary, using backup directly (task=%s)", task)
        backup_result = _try_backup_stream(
            task, messages, temp, tokens, _timeout, on_delta, prompt_hash,
            failover_trigger="circuit_open",
        )
        if backup_result is not None:
            return backup_result
        raise RuntimeError(f"Primary circuit open and backup failed (task={task})")

    t0 = time.monotonic()
    accumulated: list[str] = []
    tokens_in = tokens_out = 0
    cache_trace: dict[str, Any] = {}
    in_think = False
    first_chunk_ms: float | None = None
    first_visible_ms: float | None = None

    try:
        tracer = trace.get_tracer("agentfixbpro")
        with tracer.start_as_current_span("llm.call_stream") as span:
            _set_llm_request_span_attributes(
                span,
                task=task,
                model=model,
                messages=messages,
                max_tokens=tokens,
                timeout_seconds=_timeout,
                temperature=temp,
                streaming=True,
                max_retries=0,
                fallback_count=1,
            )

            call_kwargs = build_stream_kwargs(
                config=config, messages=messages, temperature=temp,
                max_tokens=tokens, timeout_sec=_timeout,
            )

            stream_iter = litellm.completion(**call_kwargs)

            for chunk in stream_iter:
                if first_chunk_ms is None:
                    first_chunk_ms = (time.monotonic() - t0) * 1000
                choice = chunk.choices[0] if chunk.choices else None
                if choice and hasattr(choice, "delta"):
                    delta_text = choice.delta.content or ""
                else:
                    delta_text = ""

                # Track <think> blocks ??suppress deltas inside them
                if "<think>" in delta_text:
                    in_think = True
                if "</think>" in delta_text:
                    in_think = False
                    # Drop the </think> tag itself from accumulated
                    delta_text = delta_text.split("</think>", 1)[-1]

                if delta_text and not in_think:
                    if first_visible_ms is None:
                        first_visible_ms = (time.monotonic() - t0) * 1000
                    accumulated.append(delta_text)
                    if on_delta is not None:
                        on_delta(delta_text)

                # Extract usage from final chunk
                if hasattr(chunk, "usage") and chunk.usage:
                    tokens_in = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    tokens_out = getattr(chunk.usage, "completion_tokens", 0) or 0
                    cache_trace = _usage_cache_metadata(chunk.usage)

            latency = int((time.monotonic() - t0) * 1000)
            content = "".join(accumulated)
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(content),
                response_model=model,
                first_chunk_ms=first_chunk_ms,
                ttft_ms=first_visible_ms,
            )
            logger.info(
                "llm_call_stream task=%s model=%s tokens_in=%d tokens_out=%d latency=%dms",
                task, model, tokens_in, tokens_out, latency,
            )
            _record_trace(
                task,
                model,
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=prompt_hash,
                output_text=content,
                messages=messages,
                ttft_ms=first_visible_ms,
                request_metadata=call_kwargs,
                streaming=True,
                **cache_trace,
            )
            status_tracker.record_success("primary", latency)
            return content

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000) if 't0' in locals() else 0
        content = "".join(accumulated)
        if content:
            logger.warning("llm_call_stream partial result (%d chars) task=%s error=%s",
                           len(content), task, e)
            _record_trace(
                task,
                model,
                tokens_in,
                tokens_out,
                latency,
                error=e,
                prompt_hash=prompt_hash,
                output_text=content,
                messages=messages,
                ttft_ms=first_visible_ms,
                request_metadata=locals().get("call_kwargs"),
                decision_outcome="partial_response",
                streaming=True,
                **cache_trace,
            )
            return content
        logger.error("llm_call_stream failed task=%s model=%s error=%s", task, model, e)
        _record_trace(
            task,
            model,
            0,
            0,
            latency,
            error=e,
            prompt_hash=prompt_hash,
            messages=messages,
            request_metadata=locals().get("call_kwargs"),
            ttft_ms=first_visible_ms,
            streaming=True,
            **cache_trace,
        )

        if should_failover_to_backup(e):
            status_tracker.record_failure("primary", str(e))
            backup_result = _try_backup_stream(
                task, messages, temp, tokens, _timeout, on_delta, prompt_hash,
                failover_trigger="primary_error",
            )
            if backup_result is not None:
                return backup_result

        push_on_error(task, e, get_trace_ids()[-1] if get_trace_ids() else None)
        raise


def llm_call_with_tools(
    task: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    model_override: str | None = None,
    temperature: float | None = None,
    timeout_sec: int | None = None,
) -> dict:
    """
    Make an LLM call with tool/function calling.

    Args:
        task: Task type key for model routing.
        messages: OpenAI-format messages list.
        tools: OpenAI-format tools list.
        model_override: Force a specific model.
        temperature: Override default temperature.
        notify_on_error: Forwarded to the text-call fallback. Set False for recoverable attempts.

    Returns:
        The full response message dict (may contain tool_calls).
    """
    config = get_model_for_task(task)
    model = model_override or config.primary
    temp = temperature if temperature is not None else config.temperature

    # Trim/cap once, up front, so the recorded prompt_hash reflects the
    # messages actually sent to the provider (previously the hash was computed
    # from untrimmed messages while the trimmed ones were sent).
    messages = _trim_messages_to_fit(messages, tools)
    effective_max_tokens = _cap_max_tokens(config.max_tokens, messages, tools)
    prompt_hash = hash_prompt(json.dumps(messages, ensure_ascii=False))
    _timeout = timeout_sec if timeout_sec is not None else LLM_CALL_TIMEOUT_SEC

    if not model_override and status_tracker.is_circuit_open("primary"):
        logger.info("llm_call_with_tools circuit open ??skipping primary, using backup directly (task=%s)", task)
        backup_result = _try_backup_with_tools(
            task, messages, tools, temp, effective_max_tokens, _timeout, prompt_hash,
            failover_trigger="circuit_open",
        )
        if backup_result is not None:
            return backup_result
        # Backup also failed while the primary circuit is open ??do not fall
        # through to the known-bad primary (wastes a full timeout cycle).
        raise RuntimeError(f"Primary circuit open and backup failed (task={task})")

    try:
        tracer = trace.get_tracer("agentfixbpro")
        with tracer.start_as_current_span("llm.call_with_tools") as span:
            _set_llm_request_span_attributes(
                span,
                task=task,
                model=model,
                messages=messages,
                tools=tools,
                max_tokens=effective_max_tokens,
                timeout_seconds=_timeout,
                temperature=temp,
                streaming=False,
                max_retries=config.max_retries,
                fallback_count=len(config.fallbacks or ()),
                model_override=model_override is not None,
            )
            t0 = time.monotonic()
            call_kwargs = _build_completion_kwargs(
                config=config, model=model, messages=messages, tools=tools,
                temperature=temp, max_tokens=effective_max_tokens,
                model_override=model_override,
            )
            msg_str = json.dumps(call_kwargs.get("messages", []), indent=2)
            logger.debug("LLM CALL WITH TOOLS [%s] | chars: %d | messages:\n%s", task, len(msg_str), msg_str)
            response = call_with_timeout(lambda: litellm.completion(**call_kwargs), _timeout)
            latency = int((time.monotonic() - t0) * 1000)
            msg = response.choices[0].message
            tokens_in = response.usage.prompt_tokens
            tokens_out = response.usage.completion_tokens
            msg_dict = msg.model_dump()
            serialized = json.dumps(
                msg_dict, ensure_ascii=False, sort_keys=True, default=str
            )
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(serialized),
                response_model=str(response.model),
                tool_call_count=len(msg.tool_calls) if msg.tool_calls else 0,
            )
            logger.info(
                "llm_call_with_tools task=%s model=%s tool_calls=%d",
                task, response.model,
                len(msg.tool_calls) if msg.tool_calls else 0,
            )
            _record_trace(
                task,
                response.model,
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=prompt_hash,
                output_text=serialized,
                messages=messages,
                request_metadata=call_kwargs,
                **_response_trace_metadata(response, requested_model=model),
            )
            status_tracker.record_success("primary", latency)
            return msg_dict

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000) if 't0' in locals() else 0
        logger.error("llm_call_with_tools failed task=%s model=%s error=%s", task, model, e)
        _record_trace(
            task,
            model,
            0,
            0,
            latency,
            error=e,
            prompt_hash=prompt_hash,
            messages=messages,
            request_metadata=locals().get("call_kwargs"),
            retry_count=_response_retry_count(e),
        )

        if should_failover_to_backup(e) and not model_override:
            status_tracker.record_failure("primary", str(e))
            backup_result = _try_backup_with_tools(
                task, messages, tools, temp, effective_max_tokens, _timeout, prompt_hash,
                failover_trigger="primary_error",
            )
            if backup_result is not None:
                return backup_result

        push_on_error(task, e, get_trace_ids()[-1] if get_trace_ids() else None)
        raise


def _try_backup_with_tools(
    task: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    timeout_sec: int,
    prompt_hash: str | None,
    *,
    failover_trigger: str = "primary_error",
) -> dict | None:
    """Attempt tool call on the local Gemma backup. Returns None on failure."""
    from src.llm.routing import get_backup_config

    backup = get_backup_config()
    if not backup.api_base:
        logger.error(
            "llm_call_with_tools backup skipped task=%s ? backup_api_base / local_llm_api_base not configured",
            task,
        )
        _record_backup_configuration_error(
            task=task,
            backup=backup,
            messages=messages,
            prompt_hash=prompt_hash,
            failover_trigger=failover_trigger,
            streaming=False,
            tool_mode=True,
        )
        status_tracker.record_failure("backup", "backup endpoint not configured")
        return None
    unavailable_reason = _backup_unavailable_reason(backup)
    if unavailable_reason:
        logger.error(
            "llm_call_with_tools backup skipped task=%s model=%s reason=%s",
            task, backup.primary, unavailable_reason,
        )
        _record_backup_configuration_error(
            task=task, backup=backup, messages=messages, prompt_hash=prompt_hash,
            failover_trigger=failover_trigger, streaming=False, tool_mode=True,
            skip_reason=unavailable_reason,
        )
        return None
    logger.warning(
        "llm_call_with_tools primary failed ? falling back to backup (%s @ %s)",
        backup.primary,
        backup.api_base,
    )

    # Budget the actual converted backup request, including its tool schema.
    # Local sizing denial is not a provider attempt or a provider-error trace.
    converted_messages = _trim_messages_to_fit(_convert_system_to_user(messages), tools)
    effective_max_tokens = _cap_max_tokens(
        max_tokens or backup.max_tokens, converted_messages, tools,
    )
    prompt_hash = hash_prompt(json.dumps(converted_messages, ensure_ascii=False))
    try:
        tracer = trace.get_tracer("agentfixbpro")
        with tracer.start_as_current_span("llm.call_with_tools.backup") as span:
            _set_llm_request_span_attributes(
                span,
                task=task,
                model=backup.primary,
                messages=converted_messages,
                tools=tools,
                max_tokens=effective_max_tokens,
                timeout_seconds=timeout_sec,
                temperature=temperature,
                streaming=False,
                attempt="backup",
                attempt_number=2,
                max_retries=backup.max_retries,
                fallback_count=0,
                failover_trigger=failover_trigger,
            )
            t0 = time.monotonic()
            call_kwargs = build_backup_kwargs(
                config=backup, messages=converted_messages, tools=tools,
                temperature=temperature, max_tokens=effective_max_tokens,
            )
            response = call_with_timeout(
                lambda: litellm.completion(**call_kwargs), timeout_sec
            )
            latency = int((time.monotonic() - t0) * 1000)
            msg = response.choices[0].message
            tokens_in = response.usage.prompt_tokens
            tokens_out = response.usage.completion_tokens
            msg_dict = msg.model_dump()
            serialized = json.dumps(
                msg_dict, ensure_ascii=False, sort_keys=True, default=str
            )
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(serialized),
                response_model=str(response.model),
                tool_call_count=len(msg.tool_calls) if msg.tool_calls else 0,
            )
            logger.info(
                "llm_call_with_tools BACKUP task=%s model=%s tool_calls=%d",
                task,
                response.model,
                len(msg.tool_calls) if msg.tool_calls else 0,
            )
            _record_trace(
                task,
                f"backup:{response.model}",
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=prompt_hash,
                output_text=serialized,
                messages=converted_messages,
                request_metadata=call_kwargs,
                attempt="backup",
                attempt_number=2,
                failover_trigger=failover_trigger,
                **_response_trace_metadata(
                    response,
                    failover_from=get_model_for_task(task).primary,
                ),
            )
            status_tracker.record_success("backup", latency)
            return msg_dict
    except Exception as backup_err:
        latency = int((time.monotonic() - t0) * 1000) if "t0" in locals() else 0
        logger.error(
            "llm_call_with_tools backup also failed task=%s error=%s",
            task,
            backup_err,
        )
        _quarantine_invalid_backup_model(backup, backup_err)
        _record_trace(
            task,
            f"backup:{backup.primary}",
            0,
            0,
            latency,
            error=backup_err,
            prompt_hash=prompt_hash,
            messages=locals().get("converted_messages", messages),
            request_metadata=locals().get("call_kwargs"),
            retry_count=_response_retry_count(backup_err),
            failover_from=get_model_for_task(task).primary,
            attempt="backup",
            attempt_number=2,
            failover_trigger=failover_trigger,
        )
        status_tracker.record_failure("backup", str(backup_err))
        return None


def _try_backup_stream(
    task: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    timeout_sec: int,
    on_delta: "Any",
    prompt_hash: str | None,
    *,
    failover_trigger: str = "primary_error",
) -> str | None:
    """Attempt streaming call on the backup provider. Returns None on failure."""
    from src.llm.routing import get_backup_config

    backup = get_backup_config()
    if not backup.api_base:
        logger.error(
            "llm_call_stream backup skipped task=%s ? backup endpoint not configured",
            task,
        )
        _record_backup_configuration_error(
            task=task,
            backup=backup,
            messages=messages,
            prompt_hash=prompt_hash,
            failover_trigger=failover_trigger,
            streaming=True,
        )
        status_tracker.record_failure("backup", "backup endpoint not configured")
        return None
    unavailable_reason = _backup_unavailable_reason(backup)
    if unavailable_reason:
        logger.error(
            "llm_call_stream backup skipped task=%s model=%s reason=%s",
            task, backup.primary, unavailable_reason,
        )
        _record_backup_configuration_error(
            task=task, backup=backup, messages=messages, prompt_hash=prompt_hash,
            failover_trigger=failover_trigger, streaming=True,
            skip_reason=unavailable_reason,
        )
        return None
    logger.warning(
        "llm_call_stream primary failed ? falling back to backup (%s)",
        backup.primary,
    )

    try:
        tracer = trace.get_tracer("agentfixbpro")
        with tracer.start_as_current_span("llm.call_stream.backup") as span:
            converted_messages = _convert_system_to_user(messages)
            effective_max_tokens = max_tokens or backup.max_tokens
            _set_llm_request_span_attributes(
                span,
                task=task,
                model=backup.primary,
                messages=converted_messages,
                max_tokens=effective_max_tokens,
                timeout_seconds=timeout_sec,
                temperature=temperature,
                streaming=True,
                attempt="backup",
                attempt_number=2,
                max_retries=0,
                fallback_count=0,
                failover_trigger=failover_trigger,
            )
            t0 = time.monotonic()
            call_kwargs = build_stream_kwargs(
                config=backup, messages=converted_messages,
                temperature=temperature, max_tokens=effective_max_tokens,
                timeout_sec=timeout_sec, backup=True,
            )
            stream_iter = litellm.completion(**call_kwargs)
            accumulated: list[str] = []
            tokens_in = tokens_out = 0
            cache_trace: dict[str, Any] = {}
            in_think = False
            first_chunk_ms: float | None = None
            first_visible_ms: float | None = None

            for chunk in stream_iter:
                if first_chunk_ms is None:
                    first_chunk_ms = (time.monotonic() - t0) * 1000
                choice = chunk.choices[0] if chunk.choices else None
                delta_text = ""
                if choice and hasattr(choice, "delta"):
                    delta_text = choice.delta.content or ""

                if "<think>" in delta_text:
                    in_think = True
                if "</think>" in delta_text:
                    in_think = False
                    delta_text = delta_text.split("</think>", 1)[-1]

                if delta_text and not in_think:
                    if first_visible_ms is None:
                        first_visible_ms = (time.monotonic() - t0) * 1000
                    accumulated.append(delta_text)
                    if on_delta is not None:
                        on_delta(delta_text)

                if hasattr(chunk, "usage") and chunk.usage:
                    tokens_in = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    tokens_out = getattr(chunk.usage, "completion_tokens", 0) or 0
                    cache_trace = _usage_cache_metadata(chunk.usage)

            latency = int((time.monotonic() - t0) * 1000)
            content = "".join(accumulated)
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(content),
                response_model=backup.primary,
                first_chunk_ms=first_chunk_ms,
                ttft_ms=first_visible_ms,
            )
            logger.info(
                "llm_call_stream BACKUP task=%s model=%s tokens_in=%d tokens_out=%d",
                task,
                backup.primary,
                tokens_in,
                tokens_out,
            )
            _record_trace(
                task,
                f"backup:{backup.primary}",
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=prompt_hash,
                output_text=content,
                messages=converted_messages,
                ttft_ms=first_visible_ms,
                request_metadata=call_kwargs,
                attempt="backup",
                attempt_number=2,
                failover_trigger=failover_trigger,
                streaming=True,
                **{
                    "failover_from": get_model_for_task(task).primary,
                    **cache_trace,
                },
            )
            status_tracker.record_success("backup", latency)
            return content
    except Exception as backup_err:
        latency = int((time.monotonic() - t0) * 1000) if "t0" in locals() else 0
        partial_content = "".join(locals().get("accumulated", []))
        logger.error(
            "llm_call_stream backup also failed task=%s error=%s",
            task,
            backup_err,
        )
        _quarantine_invalid_backup_model(backup, backup_err)
        _record_trace(
            task,
            f"backup:{backup.primary}",
            locals().get("tokens_in", 0),
            locals().get("tokens_out", 0),
            latency,
            error=backup_err,
            prompt_hash=prompt_hash,
            output_text=partial_content or None,
            messages=locals().get("converted_messages", messages),
            ttft_ms=locals().get("first_visible_ms"),
            request_metadata=locals().get("call_kwargs"),
            failover_from=get_model_for_task(task).primary,
            attempt="backup",
            attempt_number=2,
            failover_trigger=failover_trigger,
            streaming=True,
            **locals().get("cache_trace", {}),
        )
        status_tracker.record_failure("backup", str(backup_err))
        return None


def llm_call_structured(
    task: str,
    messages: list[dict[str, Any]],
    response_schema: dict[str, Any],
    *,
    model_override: str | None = None,
    temperature: float | None = None,
    timeout_sec: int | None = None,
    return_metadata: bool = False,
    notify_on_error: bool = True,
) -> dict | StructuredLLMResponse:
    """
    Make an LLM call expecting structured JSON output.
    Parses the response as JSON and validates structure.

    Args:
        task: Task type key for model routing.
        messages: OpenAI-format messages list.
        response_schema: Expected JSON schema (for documentation/prompt).
        model_override: Force a specific model.
        temperature: Override default temperature.

    Returns:
        Parsed JSON dict from the LLM response.

    Raises:
        ValueError: If the response cannot be parsed as valid JSON.
    """
    # Per-call metadata must never inherit a trace/revision link from an earlier
    # structured request, including native, fallback, disabled-tracing, and
    # trace-recorder-failure paths.
    _last_inference_link_var.set(None)
    structured_text_fallback = False
    structured_text_attempt_number = 1
    selected_model = model_override or get_model_for_task(task).primary

    if task in {"triage", "truth_write_reasoning"}:
        try:
            parsed = _llm_call_structured_native(
                task, messages, response_schema,
                model_override=model_override, temperature=temperature,
                timeout_sec=timeout_sec,
            )
            if return_metadata:
                model = get_last_completed_model() or model_override or get_model_for_task(task).primary
                return StructuredLLMResponse(
            data=parsed, model=model, inference=_last_inference_link_var.get(),
        )
            return parsed
        except LLMParseFailure as native_err:
            # Only a genuine parse failure benefits from re-asking as plain text.
            logger.warning(
                "Native structured output parse-failed for task=%s (%s); "
                "falling back to text+parse",
                task, native_err,
            )
            structured_text_fallback = True
            structured_text_attempt_number = (
                2 if selected_model.startswith("anthropic/") else 3
            )
        except LLMTimeoutError:
            # Do NOT cascade a timeout into a 3rd full LLM call. The native
            # path already tried json_mode -> tool_mode, each with its own
            # timeout; surfacing the timeout to the caller is cheaper and
            # more honest than blocking ~135s more on the same provider.
            raise

    augmented_messages = _augment_messages_with_schema(
        messages,
        response_schema,
        model=selected_model,
    )

    raw = llm_call(
        task,
        augmented_messages,
        model_override=model_override,
        temperature=temperature,
        timeout_sec=timeout_sec,
        notify_on_error=notify_on_error,
        _trace_attempt=(
            "structured_text_fallback" if structured_text_fallback else "primary"
        ),
        _trace_attempt_number=structured_text_attempt_number,
        _trace_failover_trigger=(
            "structured_native_parse_error" if structured_text_fallback else None
        ),
        _trace_decision_outcome=(
            "structured_text_fallback" if structured_text_fallback else None
        ),
    )

    # Strip reasoning tags safely (preserves </think> inside JSON values)
    raw = strip_reasoning(raw)
    parsed = _parse_json_response(raw)
    if return_metadata:
        model = get_last_completed_model() or model_override or get_model_for_task(task).primary
        return StructuredLLMResponse(
            data=parsed, model=model, inference=_last_inference_link_var.get(),
        )
    return parsed


def _llm_call_structured_native(
    task: str,
    messages: list[dict[str, str]],
    response_schema: dict[str, Any],
    *,
    model_override: str | None = None,
    temperature: float | None = None,
    timeout_sec: int | None = None,
) -> dict:
    """Use provider-native structured output for triage classification."""
    config = get_model_for_task(task)
    model = model_override or config.primary
    temp = temperature if temperature is not None else config.temperature
    json_schema = _normalize_response_schema(response_schema)
    augmented_messages = _augment_messages_with_schema(
        messages,
        response_schema,
        model=model,
    )
    # Apply the context-window guard on the native path too (previously the
    # native sub-functions sent untrimmed messages with the uncapped max_tokens).
    augmented_messages = _trim_messages_to_fit(augmented_messages)
    effective_max_tokens = _cap_max_tokens(config.max_tokens, augmented_messages)
    _timeout = timeout_sec if timeout_sec is not None else LLM_CALL_TIMEOUT_SEC

    if model.startswith("anthropic/"):
        return _llm_call_structured_via_tool(
            model, augmented_messages, json_schema, config,
            model_override=model_override, temperature=temp, task=task,
            timeout_sec=_timeout, max_tokens=effective_max_tokens,
        )
    return _llm_call_structured_via_json_mode(
        model, augmented_messages, json_schema, config,
        model_override=model_override, temperature=temp, task=task,
        timeout_sec=_timeout, max_tokens=effective_max_tokens,
    )


def _llm_call_structured_via_json_mode(
    model: str,
    messages: list[dict[str, str]],
    json_schema: dict[str, Any],
    config: ModelConfig,
    *,
    model_override: str | None,
    temperature: float,
    task: str = "triage",
    timeout_sec: int = LLM_CALL_TIMEOUT_SEC,
    max_tokens: int | None = None,
) -> dict:
    """Use response_format JSON mode when the provider supports it."""
    prompt_hash = hash_prompt(json.dumps(messages, ensure_ascii=False))
    try:
        tracer = trace.get_tracer("agentfixbpro")
        with tracer.start_as_current_span("llm.call_structured.json") as span:
            effective_max_tokens = (
                max_tokens if max_tokens is not None else config.max_tokens
            )
            _set_llm_request_span_attributes(
                span,
                task=task,
                model=model,
                messages=messages,
                max_tokens=effective_max_tokens,
                timeout_seconds=timeout_sec,
                temperature=temperature,
                streaming=False,
                max_retries=config.max_retries,
                fallback_count=1,
                model_override=model_override is not None,
                structured_mode="json",
            )
            t0 = time.monotonic()
            call_kwargs = _build_completion_kwargs(
                config=config,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=effective_max_tokens,
                model_override=model_override,
            )
            add_json_mode(call_kwargs)
            # TODO: Migrate triage classification to provider-native JSON/tool mode when the selected LiteLLM provider supports it.
            msg_str = json.dumps(call_kwargs.get("messages", []), indent=2)
            logger.debug(
                "LLM CALL JSON MODE [%s] | chars: %d | messages:\n%s",
                model,
                len(msg_str),
                msg_str,
            )
            response = call_with_timeout(
                lambda: litellm.completion(**call_kwargs), timeout_sec
            )
            latency = int((time.monotonic() - t0) * 1000)
            content = response.choices[0].message.content or ""
            tokens_in = response.usage.prompt_tokens
            tokens_out = response.usage.completion_tokens
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(content),
                response_model=str(response.model),
                tool_call_count=0,
            )
            try:
                parsed = _parse_json_response(content)
            except LLMParseFailure as parse_err:
                _record_trace(
                    task,
                    response.model,
                    tokens_in,
                    tokens_out,
                    latency,
                    prompt_hash=prompt_hash,
                    error=parse_err,
                    output_text=content,
                    messages=messages,
                    request_metadata=call_kwargs,
                    attempt="structured_json",
                    attempt_number=1,
                    decision_outcome="structured_json_parse_error",
                    **_response_trace_metadata(response, requested_model=model),
                )
                raise
            _record_trace(
                task,
                response.model,
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=prompt_hash,
                output_text=content,
                messages=messages,
                request_metadata=call_kwargs,
                attempt="structured_json",
                attempt_number=1,
                decision_outcome="structured_json",
                **_response_trace_metadata(response, requested_model=model),
            )
            _last_completed_model_var.set(str(response.model))
            return parsed
    except LLMTimeoutError as exc:
        latency = int((time.monotonic() - t0) * 1000) if "t0" in locals() else 0
        _record_trace(
            task,
            model,
            0,
            0,
            latency,
            error=exc,
            prompt_hash=prompt_hash,
            messages=messages,
            request_metadata=locals().get("call_kwargs"),
            retry_count=_response_retry_count(exc),
            attempt="structured_json",
            attempt_number=1,
            decision_outcome="structured_json_timeout",
        )
        # Do not cascade a timeout into the tool-mode sub-call.
        raise
    except Exception as exc:
        # Parse failures already include the provider response in their trace.
        if not isinstance(exc, LLMParseFailure):
            latency = int((time.monotonic() - t0) * 1000) if "t0" in locals() else 0
            _record_trace(
                task,
                model,
                0,
                0,
                latency,
                error=exc,
                prompt_hash=prompt_hash,
                messages=messages,
                request_metadata=locals().get("call_kwargs"),
                retry_count=_response_retry_count(exc),
                attempt="structured_json",
                attempt_number=1,
                decision_outcome="structured_json_error",
            )
        logger.warning(
            "Native structured json mode failed for %s: %s; falling back to tool mode",
            model,
            exc,
        )
        return _llm_call_structured_via_tool(
            model,
            messages,
            json_schema,
            config,
            model_override=model_override,
            temperature=temperature,
            task=task,
            timeout_sec=timeout_sec,
            max_tokens=max_tokens,
            fallback_trigger="json_mode_error",
        )


def _llm_call_structured_via_tool(
    model: str,
    messages: list[dict[str, str]],
    json_schema: dict[str, Any],
    config: ModelConfig,
    *,
    model_override: str | None,
    temperature: float,
    task: str = "triage",
    timeout_sec: int = LLM_CALL_TIMEOUT_SEC,
    max_tokens: int | None = None,
    fallback_trigger: str | None = None,
) -> dict:
    """Use native tool/function calling for structured output."""
    structured_tools = [{
        "type": "function",
        "function": {
            "name": f"return_{task}_result",
            "description": f"Return the {task} result as structured JSON.",
            "parameters": json_schema,
        },
    }]
    # Schema text in the prompt can be trimmed; the attached tool schema cannot.
    # Validate the complete request before opening a provider-attempt span.
    messages = _trim_messages_to_fit(messages, structured_tools)
    effective_max_tokens = _cap_max_tokens(
        max_tokens if max_tokens is not None else config.max_tokens,
        messages, structured_tools,
    )
    tracer = trace.get_tracer("agentfixbpro")
    with tracer.start_as_current_span("llm.call_structured.tool") as span:
        structured_attempt = (
            "structured_tool_fallback" if fallback_trigger else "structured_tool"
        )
        attempt_number = 2 if fallback_trigger else 1
        _set_llm_request_span_attributes(
            span,
            task=task,
            model=model,
            messages=messages,
            tools=structured_tools,
            max_tokens=effective_max_tokens,
            timeout_seconds=timeout_sec,
            temperature=temperature,
            streaming=False,
            attempt=structured_attempt,
            attempt_number=attempt_number,
            max_retries=config.max_retries,
            fallback_count=len(config.fallbacks or ()),
            model_override=model_override is not None,
            failover_trigger=fallback_trigger,
            structured_mode="tool",
        )
        t0 = time.monotonic()
        prompt_hash = hash_prompt(json.dumps(messages, ensure_ascii=False))
        call_kwargs = _build_completion_kwargs(
            config=config,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=effective_max_tokens,
            model_override=model_override,
        )
        add_structured_tool(call_kwargs, structured_tools)
        msg_str = json.dumps(call_kwargs.get("messages", []), indent=2)
        logger.debug(
            "LLM CALL TOOL [%s] | chars: %d | messages:\n%s",
            model,
            len(msg_str),
            msg_str,
        )
        try:
            response = call_with_timeout(
                lambda: litellm.completion(**call_kwargs), timeout_sec
            )
            latency = int((time.monotonic() - t0) * 1000)
            tokens_in = response.usage.prompt_tokens
            tokens_out = response.usage.completion_tokens
            tool_message = _structured_tool_message(response.choices[0].message)
            raw_output = tool_message.raw_output
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(raw_output),
                response_model=str(response.model),
                tool_call_count=tool_message.tool_call_count,
            )
            try:
                parsed = tool_message.parse(task, _parse_json_response)
            except LLMParseFailure as parse_err:
                _record_trace(
                    task,
                    response.model,
                    tokens_in,
                    tokens_out,
                    latency,
                    prompt_hash=prompt_hash,
                    error=parse_err,
                    output_text=raw_output,
                    messages=messages,
                    request_metadata=call_kwargs,
                    decision_outcome=(
                        "structured_tool_fallback_parse_error"
                        if fallback_trigger
                        else "structured_tool_parse_error"
                    ),
                    attempt=structured_attempt,
                    attempt_number=attempt_number,
                    failover_trigger=fallback_trigger,
                    **_response_trace_metadata(response, requested_model=model),
                )
                raise
            output_text = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
            _record_trace(
                task,
                response.model,
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=prompt_hash,
                output_text=output_text,
                messages=messages,
                request_metadata=call_kwargs,
                decision_outcome=(
                    "structured_tool_fallback"
                    if fallback_trigger
                    else "structured_tool"
                ),
                attempt=structured_attempt,
                attempt_number=attempt_number,
                failover_trigger=fallback_trigger,
                **_response_trace_metadata(response, requested_model=model),
            )
            _last_completed_model_var.set(str(response.model))
            return parsed
        except LLMParseFailure:
            raise
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            _record_trace(
                task,
                model,
                0,
                0,
                latency,
                error=exc,
                prompt_hash=prompt_hash,
                messages=messages,
                request_metadata=call_kwargs,
                retry_count=_response_retry_count(exc),
                decision_outcome=(
                    "structured_tool_fallback_error"
                    if fallback_trigger
                    else "structured_tool_error"
                ),
                attempt=structured_attempt,
                attempt_number=attempt_number,
                failover_trigger=fallback_trigger,
            )
            raise


def _build_completion_kwargs(
    *,
    config: ModelConfig,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    model_override: str | None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # Keep the facade-level seam for call-time monkeypatches.
    return build_completion_kwargs(
        config=config, model=model, messages=messages, temperature=temperature,
        max_tokens=max_tokens, model_override=model_override, tools=tools,
    )


def _parse_json_response(raw: str) -> dict:
    """Keep the facade's logging identity and call-time patch surface."""
    return _parse_structured_json(raw, log=logger)


# ------------------------------------------------------------------
# Vision / multimodal helpers
# ------------------------------------------------------------------

def llm_describe_image(
    task: str,
    prompt: str,
    image_base64: str,
    content_type: str = "image/png",
) -> str:
    """Call the LLM with a vision-capable multimodal message.

    Constructs an OpenAI-compatible message with image_url content part
    (base64 data URI) and returns the model's text description.

    Args:
        task: Task type key for model routing.
        prompt: Text prompt to accompany the image.
        image_base64: Base64-encoded image data.
        content_type: MIME type of the image (e.g. "image/png", "image/jpeg").

    Returns:
        The model's text description of the image.
    """
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{content_type};base64,{image_base64}",
                    },
                },
            ],
        }
    ]
    return llm_call(task, messages)
