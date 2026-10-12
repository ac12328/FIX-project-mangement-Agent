"""Streaming LLM completion with tool-call assembly."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from src.llm import litellm_transport as litellm
from opentelemetry import trace

from src.llm import status_tracker
from src.llm.failover import should_failover_to_backup
from src.llm.routing import get_backup_config, get_model_for_task
from src.llm.watchdog import LLM_CALL_TIMEOUT_SEC
from src.observability.traces import hash_prompt

logger = logging.getLogger(__name__)


def _merge_tool_call_delta(accumulator: dict[int, dict], raw_delta: Any) -> None:
    """Merge one streamed tool-call fragment into *accumulator* keyed by index."""
    if raw_delta is None:
        return
    index = getattr(raw_delta, "index", None)
    if index is None:
        return
    entry = accumulator.setdefault(
        index,
        {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    delta_id = getattr(raw_delta, "id", None)
    if delta_id:
        entry["id"] = delta_id
    fn = getattr(raw_delta, "function", None)
    if fn is None:
        return
    name = getattr(fn, "name", None)
    if name:
        entry["function"]["name"] += name
    arguments = getattr(fn, "arguments", None)
    if arguments:
        entry["function"]["arguments"] += arguments


def _assemble_tool_calls(accumulator: dict[int, dict]) -> list[dict] | None:
    if not accumulator:
        return None
    calls = [accumulator[idx] for idx in sorted(accumulator)]
    cleaned: list[dict] = []
    for idx, call in enumerate(calls):
        fn = call.get("function") or {}
        name = (fn.get("name") or "").strip()
        if not name:
            continue
        cleaned.append(
            {
                "id": call.get("id") or f"call_{idx}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": fn.get("arguments") or "{}",
                },
            }
        )
    return cleaned or None


def llm_call_with_tools_stream(
    task: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    on_delta: Callable[[str], None] | None = None,
    model_override: str | None = None,
    temperature: float | None = None,
    timeout_sec: int | None = None,
    trim_fn: Callable[..., Any] | None = None,
    cap_tokens_fn: Callable[..., int | None] | None = None,
) -> dict:
    """Streaming variant of tool calling — emits visible content deltas live."""
    from src.llm.client import (
        _record_trace,
        _set_llm_request_span_attributes,
        _usage_cache_metadata,
        _set_llm_response_span_attributes,
    )

    config = get_model_for_task(task)
    model = model_override or config.primary
    temp = temperature if temperature is not None else config.temperature
    _timeout = timeout_sec if timeout_sec is not None else LLM_CALL_TIMEOUT_SEC
    if trim_fn is not None:
        messages = trim_fn(messages, tools)
    effective_max_tokens = None
    if cap_tokens_fn is not None:
        effective_max_tokens = cap_tokens_fn(config.max_tokens, messages, tools)
    prompt_hash = hash_prompt(json.dumps(messages, ensure_ascii=False))

    t0 = time.monotonic()
    accumulated: list[str] = []
    tool_calls_acc: dict[int, dict] = {}
    tokens_in = tokens_out = 0
    cache_trace: dict[str, Any] = {}
    in_think = False
    emitted_visible = False
    first_chunk_ms: float | None = None
    first_visible_ms: float | None = None

    try:
        tracer = trace.get_tracer("agentfixbpro")
        with tracer.start_as_current_span("llm.call_with_tools_stream") as span:
            _set_llm_request_span_attributes(
                span,
                task=task,
                model=model,
                messages=messages,
                tools=tools,
                max_tokens=effective_max_tokens,
                timeout_seconds=_timeout,
                temperature=temp,
                streaming=True,
                max_retries=0,
                fallback_count=len(config.fallbacks or ()),
                model_override=model_override is not None,
            )

            call_kwargs: dict = dict(
                model=model,
                messages=messages,
                tools=tools,
                temperature=temp,
                max_tokens=effective_max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                timeout=_timeout,
            )
            if not model_override:
                call_kwargs["fallbacks"] = config.fallbacks
            if config.api_base:
                call_kwargs["api_base"] = config.api_base
            if config.api_key:
                call_kwargs["api_key"] = config.api_key

            for chunk in litellm.completion(**call_kwargs):
                if first_chunk_ms is None:
                    first_chunk_ms = (time.monotonic() - t0) * 1000
                choice = chunk.choices[0] if chunk.choices else None
                if choice is None:
                    continue
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                delta_text = getattr(delta, "content", None) or ""
                if "<think>" in delta_text:
                    in_think = True
                if "</think>" in delta_text:
                    in_think = False
                    delta_text = delta_text.split("</think>", 1)[-1]

                if delta_text and not in_think:
                    if first_visible_ms is None:
                        first_visible_ms = (time.monotonic() - t0) * 1000
                    accumulated.append(delta_text)
                    emitted_visible = True
                    if on_delta is not None:
                        on_delta(delta_text)

                for tc_delta in getattr(delta, "tool_calls", None) or []:
                    _merge_tool_call_delta(tool_calls_acc, tc_delta)

                if hasattr(chunk, "usage") and chunk.usage:
                    tokens_in = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    tokens_out = getattr(chunk.usage, "completion_tokens", 0) or 0
                    cache_trace = _usage_cache_metadata(chunk.usage)

            latency = int((time.monotonic() - t0) * 1000)
            content = "".join(accumulated) or None
            tool_calls = _assemble_tool_calls(tool_calls_acc)
            serialized_response = json.dumps(
                {"content": content, "tool_calls": tool_calls},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(serialized_response),
                response_model=model,
                tool_call_count=len(tool_calls) if tool_calls else 0,
                first_chunk_ms=first_chunk_ms,
                ttft_ms=first_visible_ms,
            )
            logger.info(
                "llm_call_with_tools_stream task=%s model=%s tool_calls=%d emitted=%s",
                task,
                model,
                len(tool_calls) if tool_calls else 0,
                emitted_visible,
            )
            _record_trace(
                task,
                model,
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=prompt_hash,
                output_text=serialized_response,
                messages=messages,
                ttft_ms=first_visible_ms,
                request_metadata=call_kwargs,
                streaming=True,
                **cache_trace,
            )
            status_tracker.record_success("primary", latency)
            return {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }

    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(
            "llm_call_with_tools_stream failed task=%s model=%s error=%s",
            task,
            model,
            exc,
        )
        partial_response = json.dumps(
            {
                "content": "".join(accumulated) or None,
                "tool_calls": _assemble_tool_calls(tool_calls_acc),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        _record_trace(
            task,
            model,
            tokens_in,
            tokens_out,
            latency,
            error=exc,
            prompt_hash=prompt_hash,
            output_text=partial_response,
            messages=messages,
            ttft_ms=first_visible_ms,
            request_metadata=locals().get("call_kwargs"),
            streaming=True,
            **cache_trace,
        )

        if should_failover_to_backup(exc) and not model_override:
            status_tracker.record_failure("primary", str(exc))
            backup_result = _try_backup_with_tools_stream(
                task=task,
                messages=messages,
                tools=tools,
                temperature=temp,
                timeout_sec=_timeout,
                trim_fn=trim_fn,
                cap_tokens_fn=cap_tokens_fn,
                prompt_hash=prompt_hash,
                failover_trigger="primary_error",
            )
            if backup_result is not None:
                return backup_result

        raise


def _try_backup_with_tools_stream(
    *,
    task: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: float,
    timeout_sec: int,
    trim_fn: Callable[..., Any] | None,
    cap_tokens_fn: Callable[..., int | None] | None,
    prompt_hash: str | None,
    failover_trigger: str = "primary_error",
) -> dict | None:
    """Retry a streaming tool call on the backup provider."""
    from src.llm.client import (
        _convert_system_to_user,
        _backup_unavailable_reason,
        _quarantine_invalid_backup_model,
        _record_backup_configuration_error,
        _record_trace,
        _set_llm_request_span_attributes,
        _set_llm_response_span_attributes,
        _usage_cache_metadata,
    )

    backup = get_backup_config()
    if not backup.api_base:
        logger.error(
            "llm_call_with_tools_stream backup skipped task=%s ? backup endpoint not configured",
            task,
        )
        _record_backup_configuration_error(
            task=task,
            backup=backup,
            messages=messages,
            prompt_hash=prompt_hash,
            failover_trigger=failover_trigger,
            streaming=True,
            tool_mode=True,
        )
        status_tracker.record_failure("backup", "backup endpoint not configured")
        return None
    unavailable_reason = _backup_unavailable_reason(backup)
    if unavailable_reason:
        logger.error(
            "llm_call_with_tools_stream backup skipped task=%s model=%s reason=%s",
            task, backup.primary, unavailable_reason,
        )
        _record_backup_configuration_error(
            task=task, backup=backup, messages=messages, prompt_hash=prompt_hash,
            failover_trigger=failover_trigger, streaming=True, tool_mode=True,
            skip_reason=unavailable_reason,
        )
        return None
    logger.warning(
        "llm_call_with_tools_stream primary failed ? falling back to backup (%s)",
        backup.primary,
    )

    if trim_fn is not None:
        messages = trim_fn(messages, tools)
    converted_messages = _convert_system_to_user(messages)
    effective_max_tokens = None
    if cap_tokens_fn is not None:
        effective_max_tokens = cap_tokens_fn(
            backup.max_tokens, converted_messages, tools
        )
    backup_prompt_hash = hash_prompt(
        json.dumps(converted_messages, ensure_ascii=False)
    ) or prompt_hash

    try:
        tracer = trace.get_tracer("agentfixbpro")
        with tracer.start_as_current_span("llm.call_with_tools_stream.backup") as span:
            _set_llm_request_span_attributes(
                span,
                task=task,
                model=backup.primary,
                messages=converted_messages,
                tools=tools,
                max_tokens=effective_max_tokens,
                timeout_seconds=timeout_sec,
                temperature=temperature,
                streaming=True,
                attempt="backup",
                attempt_number=2,
                max_retries=backup.max_retries,
                fallback_count=0,
                failover_trigger=failover_trigger,
            )
            t0 = time.monotonic()
            accumulated: list[str] = []
            tool_calls_acc: dict[int, dict] = {}
            tokens_in = tokens_out = 0
            cache_trace: dict[str, Any] = {}
            in_think = False
            first_chunk_ms: float | None = None
            first_visible_ms: float | None = None

            call_kwargs: dict = dict(
                model=backup.primary,
                messages=converted_messages,
                tools=tools,
                temperature=temperature,
                max_tokens=effective_max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                timeout=timeout_sec,
                api_base=backup.api_base,
                api_key=backup.api_key,
                max_retries=backup.max_retries,
            )

            for chunk in litellm.completion(**call_kwargs):
                if first_chunk_ms is None:
                    first_chunk_ms = (time.monotonic() - t0) * 1000
                choice = chunk.choices[0] if chunk.choices else None
                if choice is None:
                    continue
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                delta_text = getattr(delta, "content", None) or ""
                if "<think>" in delta_text:
                    in_think = True
                if "</think>" in delta_text:
                    in_think = False
                    delta_text = delta_text.split("</think>", 1)[-1]

                if delta_text and not in_think:
                    if first_visible_ms is None:
                        first_visible_ms = (time.monotonic() - t0) * 1000
                    accumulated.append(delta_text)

                for tc_delta in getattr(delta, "tool_calls", None) or []:
                    _merge_tool_call_delta(tool_calls_acc, tc_delta)

                if hasattr(chunk, "usage") and chunk.usage:
                    tokens_in = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    tokens_out = getattr(chunk.usage, "completion_tokens", 0) or 0
                    cache_trace = _usage_cache_metadata(chunk.usage)

            latency = int((time.monotonic() - t0) * 1000)
            content = "".join(accumulated) or None
            tool_calls = _assemble_tool_calls(tool_calls_acc)
            serialized = json.dumps(
                {"content": content, "tool_calls": tool_calls},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            _set_llm_response_span_attributes(
                span,
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                output_chars=len(serialized),
                response_model=backup.primary,
                tool_call_count=len(tool_calls) if tool_calls else 0,
                first_chunk_ms=first_chunk_ms,
                ttft_ms=first_visible_ms,
            )
            logger.info(
                "llm_call_with_tools_stream BACKUP task=%s model=%s tool_calls=%d",
                task,
                backup.primary,
                len(tool_calls) if tool_calls else 0,
            )
            _record_trace(
                task,
                f"backup:{backup.primary}",
                tokens_in,
                tokens_out,
                latency,
                prompt_hash=backup_prompt_hash,
                output_text=serialized,
                messages=converted_messages,
                ttft_ms=first_visible_ms,
                failover_from=get_model_for_task(task).primary,
                request_metadata=call_kwargs,
                attempt="backup",
                attempt_number=2,
                failover_trigger=failover_trigger,
                streaming=True,
                **cache_trace,
            )
            status_tracker.record_success("backup", latency)
            return {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }
    except Exception as backup_err:
        latency = int((time.monotonic() - t0) * 1000) if "t0" in locals() else 0
        partial_response = json.dumps(
            {
                "content": "".join(locals().get("accumulated", [])) or None,
                "tool_calls": _assemble_tool_calls(
                    locals().get("tool_calls_acc", {})
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        logger.error(
            "llm_call_with_tools_stream backup also failed task=%s error=%s",
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
            prompt_hash=backup_prompt_hash,
            output_text=partial_response,
            messages=converted_messages,
            ttft_ms=locals().get("first_visible_ms"),
            failover_from=get_model_for_task(task).primary,
            request_metadata=locals().get("call_kwargs"),
            attempt="backup",
            attempt_number=2,
            failover_trigger=failover_trigger,
            streaming=True,
            **locals().get("cache_trace", {}),
        )
        status_tracker.record_failure("backup", str(backup_err))
        return None
