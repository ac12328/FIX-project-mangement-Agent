"""
Transport-agnostic multi-round agent loop.

Single entry point: ``run_agent_loop(config) -> LoopResult``.
Harness layers live in ``harness.py``; this file is the immutable loop skeleton.
"""

from __future__ import annotations

import logging
import time
from contextlib import nullcontext

from opentelemetry import trace

from src.agent.harness import (
    collect_think_reasoning,
    harness_execute_tools,
    harness_exhaustion,
    harness_finalize,
    harness_llm_error,
    harness_pre_process,
    harness_tool_nudge,
    init_loop_state,
    normalize_response,
    try_staged_confirm_shortcut,
)
from src.agent.loop_config import LoopConfig, LoopResult
from src.agent.safety import summarise_on_exhaustion
from src.llm.client import llm_call_with_tools as _client_llm_call_with_tools
from src.llm.tool_stream import llm_call_with_tools_stream
from src.context.compression import micro_compact_tool_results
from src.observability.contracts import (
    TraceContext,
    current_trace_context,
    round_index_scope,
    trace_context_scope,
)
from src.observability.lifecycle import emit_event, emit_run_finish, emit_run_start
from src.tools.executor import execute_tool_calls
from src.tools.health_tracker import get_tracker

logger = logging.getLogger(__name__)

_TOOL_SELECTION_FUNNEL_KEYS = (
    "selected",
    "authorized",
    "blocked",
    "executed",
    "succeeded",
    "failed",
    "denied",
    "duplicate_selection",
)


def _empty_tool_selection_funnel() -> dict[str, int]:
    return {key: 0 for key in _TOOL_SELECTION_FUNNEL_KEYS}


def _set_tool_selection_span_attributes(
    span: trace.Span,
    funnel: dict[str, int],
) -> None:
    for key in _TOOL_SELECTION_FUNNEL_KEYS:
        span.set_attribute(
            f"agent.tool_selection.{key}",
            int(funnel.get(key, 0)),
        )

__all__ = [
    "execute_tool_calls",
    "invoke_llm_round",
    "llm_call_with_tools",
    "llm_call_with_tools_stream",
    "micro_compact_tool_results",
    "run_agent_loop",
    "summarise_on_exhaustion",
]


def llm_call_with_tools(task, messages, tools, temperature=0.3, **kwargs):
    """Patch target for tests; delegates to LiteLLM client."""
    return _client_llm_call_with_tools(
        task, messages, tools, temperature=temperature, **kwargs,
    )


def invoke_llm_round(config: LoopConfig, messages, tools):
    """One LLM round with an explicit primary inference surface."""
    from src.memory.exposure_context import inference_scope

    with inference_scope(
        operator_id=config.operator_id or "default_operator", surface=config.surface,
        task_type=config.intent, project_id=config.effective_project_id,
        candidate_scopes=config.exposure_scopes,
    ):
        return _invoke_llm_round_scoped(config, messages, tools)


def _invoke_llm_round_scoped(config: LoopConfig, messages, tools):
    from src.llm.watchdog import LLM_REASONING_TIMEOUT_SEC

    timeout_sec = float(LLM_REASONING_TIMEOUT_SEC)
    if config.deadline_monotonic is not None:
        remaining = config.deadline_monotonic - time.monotonic()
        if tools:
            remaining -= config.finalization_reserve_seconds
        if remaining <= 0:
            phase = "retrieval" if tools else "finalization"
            raise TimeoutError(f"agent {phase} budget exhausted")
        timeout_sec = max(0.1, min(timeout_sec, remaining))

    if tools:
        return llm_call_with_tools(
            "reasoning", messages, tools, temperature=config.temperature,
            timeout_sec=timeout_sec,
        )
    if config.on_stream_delta is None:
        return llm_call_with_tools(
            "reasoning", messages, tools, temperature=config.temperature,
            timeout_sec=timeout_sec,
        )
    content_cb = config.on_stream_delta

    def _on_content_delta(text: str) -> None:
        if content_cb is not None:
            content_cb(text)

    try:
        result = llm_call_with_tools_stream(
            "reasoning",
            messages,
            tools,
            on_delta=_on_content_delta,
            temperature=config.temperature,
            timeout_sec=timeout_sec,
            trim_fn=_trim_messages,
            cap_tokens_fn=_cap_tokens,
        )
        config.stream_flags["final_answer_streamed"] = True
        return result
    except Exception:
        logger.warning(
            "%s: streaming LLM call failed — falling back to non-streaming",
            config.caller_name,
            exc_info=True,
        )
        config.stream_flags["final_answer_streamed"] = False
        return llm_call_with_tools(
            "reasoning", messages, tools, temperature=config.temperature,
            timeout_sec=timeout_sec,
        )


def _trim_messages(messages, model):
    from src.llm.client import _trim_messages_to_fit
    return _trim_messages_to_fit(messages, model)


def _cap_tokens(model, requested):
    from src.llm.client import _cap_max_tokens
    return _cap_max_tokens(model, requested)


def run_agent_loop(config: LoopConfig) -> LoopResult:
    """Multi-round agent loop shared across all surfaces."""
    tracer = trace.get_tracer("agentfixbpro.agent")
    with tracer.start_as_current_span("agent.loop") as span:
        span.set_attribute("agent.surface", str(config.surface))
        span.set_attribute("agent.intent", str(config.intent or ""))
        span.set_attribute("agent.caller", str(config.caller_name or ""))
        span.set_attribute("agent.max_rounds", int(config.max_rounds))
        span.set_attribute("agent.message_count", len(config.messages))
        span.set_attribute("agent.tool_count", len(config.tools))
        span.set_attribute("agent.project_scoped", bool(config.effective_project_id))
        result = _run_agent_loop_traced(config)
        span.set_attribute("agent.exit_reason", str(result.exit_reason or ""))
        return result


def _run_agent_loop_traced(config: LoopConfig) -> LoopResult:
    from src.agent.project_scope import reset_forced_project_id, set_forced_project_id

    if not (config.caller_name or "").startswith("specialist:"):
        from src.agent.specialists.delegate_policy import reset_host_delegate_ledger

        reset_host_delegate_ledger()

    force_pid = (
        config.effective_project_id
        if getattr(config, "project_scope_forced", False)
        else None
    )
    scope_token = set_forced_project_id(force_pid)
    active_context = current_trace_context()
    context = config.trace_context or active_context
    owns_context = context is None
    if context is None:
        context = TraceContext.create(
            surface=str(config.surface),
            task=config.caller_name or config.intent or "agent_loop",
            thread_id=config.thread_id,
            implicit=True,
        )
    bind_scope = trace_context_scope(context) if active_context is not context else nullcontext(context)
    try:
        with bind_scope:
            if owns_context:
                emit_run_start(
                    context,
                    metadata={
                        "surface": str(config.surface),
                        "task": config.caller_name or config.intent or "agent_loop",
                        "project_id": config.effective_project_id,
                        "session_id": config.thread_id,
                        "chat_id": config.chat_id,
                        "operator_id": config.operator_id,
                        "intent": config.intent,
                        "max_rounds": config.max_rounds,
                    },
                )
            try:
                result = _run_agent_loop_body(config)
                if owns_context:
                    emit_run_finish(
                        context,
                        status="completed",
                        exit_reason=result.exit_reason,
                        final_output=result.reply,
                        persistence_status="not_owned",
                        anomaly_flags=["loop_exhaustion"] if result.exit_reason in {"budget_exhausted", "exhausted"} else [],
                        metadata={
                            "round_count": result.round_count,
                            "guard_results": result.guard_results,
                        },
                    )
                return result
            except Exception as exc:
                if owns_context:
                    emit_run_finish(
                        context,
                        status="error",
                        exit_reason=type(exc).__name__,
                        persistence_status="not_owned",
                        anomaly_flags=["error"],
                        metadata={"error_class": type(exc).__name__},
                    )
                raise
    finally:
        reset_forced_project_id(scope_token)

def _emit_checkpoint(
    config: LoopConfig,
    state,
    *,
    phase: str,
    round_index: int,
    exit_reason: str | None = None,
) -> None:
    """Publish crash-safe loop state without making persistence a dependency."""
    callback = config.on_checkpoint
    if callback is None:
        return
    payload = {
        "schema_version": 1,
        "phase": phase,
        "round_index": max(0, int(round_index)),
        "exit_reason": exit_reason,
        "messages": list(state.messages),
        "tool_history": list(state.tool_history),
        "execution_receipts": [
            receipt.to_dict() for receipt in state.execution_receipts
        ],
        "guard_results": list(state.guard_results),
    }
    try:
        callback(payload)
    except Exception:
        logger.warning(
            "%s: checkpoint callback failed during %s",
            config.caller_name,
            phase,
            exc_info=True,
        )


def _deadline_loop_result(config: LoopConfig, state, round_count: int) -> LoopResult:
    result = LoopResult(
        reply=(
            '{"ok":false,"completion_status":"deadline_exceeded",'
            '"error":"execution_deadline_exceeded",'
            '"summary":"Agent execution deadline exceeded before completion."}'
        ),
        messages=state.messages,
        round_count=round_count,
        tool_history=state.tool_history,
        exit_reason="deadline_exceeded",
        execution_receipts=state.execution_receipts,
        guard_results=list(state.guard_results),
    )
    _emit_checkpoint(
        config,
        state,
        phase="deadline",
        round_index=round_count,
        exit_reason=result.exit_reason,
    )
    return result


def _run_agent_loop_body(config: LoopConfig) -> LoopResult:
    from src.llm.client import _estimate_tokens
    from src.observability.context_budget import record_loop_budget

    record_loop_budget(
        config.caller_name,
        config.messages,
        config.tools,
        estimate_fn=_estimate_tokens,
    )

    max_rounds = config.max_rounds
    budget_warning_round = max(1, int(max_rounds * 0.7))
    state = init_loop_state(config)

    shortcut = try_staged_confirm_shortcut(config, state)
    if shortcut is not None:
        _emit_checkpoint(
            config,
            state,
            phase="final",
            round_index=shortcut.round_count,
            exit_reason=shortcut.exit_reason,
        )
        return shortcut

    tracer = trace.get_tracer("agentfixbpro.agent")
    context = current_trace_context()
    for round_num in range(max_rounds):
        if (
            config.deadline_monotonic is not None
            and time.monotonic() >= config.deadline_monotonic
        ):
            return _deadline_loop_result(config, state, round_num)
        remaining = config.remaining_seconds()
        if (
            remaining is not None
            and config.tools
            and not state.force_respond
            and remaining <= config.finalization_reserve_seconds
        ):
            state.force_respond = True
            state.exit_reason = "finalization_reserve"
            state.messages.append({
                "role": "system",
                "content": (
                    "The retrieval/tool budget is closed. Use the remaining time "
                    "only for a final answer based on evidence already collected. "
                    "Do not call tools."
                ),
            })
            state.guard_results.append({
                "guard": "finalization_reserve",
                "outcome": "force_final_response",
                "remaining_seconds": max(0.0, remaining),
            })
        round_index = round_num + 1
        with round_index_scope(round_index):
            with tracer.start_as_current_span("agent.round") as round_span:
                round_span.set_attribute("agent.round.index", round_index)
                round_span.set_attribute("agent.round.max", max_rounds)
                round_span.set_attribute("agent.message_count", len(state.messages))
                guard_results_start = len(state.guard_results)
                harness_pre_process(
                    config,
                    state,
                    round_num,
                    max_rounds=max_rounds,
                    budget_warning_round=budget_warning_round,
                )
                reserved_final_round = (
                    config.reserve_final_response_round
                    and round_num == max_rounds - 1
                )
                if reserved_final_round and not state.force_respond:
                    state.messages.append({
                        "role": "system",
                        "content": (
                            "This is the final response round. Do not call tools. "
                            "Return the complete final answer now using the required output contract, "
                            "based only on evidence already collected."
                        ),
                    })
                active_tools = [] if state.force_respond or reserved_final_round else config.tools
                active_tool_names = [
                    str(tool.get("function", {}).get("name") or "")
                    for tool in active_tools
                    if tool.get("function", {}).get("name")
                ]
                get_tracker().record_exposure(active_tool_names)
                selection_funnel = _empty_tool_selection_funnel()
                round_span.set_attribute("agent.active_tool_count", len(active_tools))
                round_span.set_attribute("agent.force_respond", bool(state.force_respond))
                if context is not None:
                    emit_event(
                        context,
                        "round.start",
                        status="running",
                        metadata={
                            "round_budget": max_rounds,
                            "message_count": len(state.messages),
                            "active_tool_count": len(active_tools),
                            "active_tool_names": active_tool_names,
                            "force_respond": bool(state.force_respond),
                            "reserved_final_round": reserved_final_round,
                        },
                    )
                try:
                    response = invoke_llm_round(config, state.messages, active_tools)
                except Exception as exc:
                    if (
                        config.deadline_monotonic is not None
                        and time.monotonic() >= config.deadline_monotonic
                    ):
                        return _deadline_loop_result(config, state, round_num + 1)
                    if (
                        isinstance(exc, TimeoutError)
                        and active_tools
                        and config.deadline_monotonic is not None
                        and config.finalization_reserve_seconds > 0
                        and time.monotonic() < config.deadline_monotonic
                    ):
                        state.force_respond = True
                        state.exit_reason = "finalization_reserve"
                        state.messages.append({
                            "role": "system",
                            "content": (
                                "The retrieval reasoning phase timed out, but the "
                                "finalization reserve remains. Return a tool-free "
                                "final answer now using retained evidence."
                            ),
                        })
                        state.guard_results.append({
                            "guard": "finalization_reserve",
                            "outcome": "retrieval_timeout_recovered",
                            "error_class": type(exc).__name__,
                        })
                        _emit_checkpoint(
                            config,
                            state,
                            phase="retrieval_timeout_recovered",
                            round_index=round_index,
                            exit_reason="finalization_reserve",
                        )
                        continue
                    round_span.record_exception(exc)
                    round_span.set_attribute("agent.round.error", type(exc).__name__)
                    logger.exception(
                        "%s: LLM call failed in round %d",
                        config.caller_name,
                        round_index,
                    )
                    if context is not None:
                        emit_event(
                            context,
                            "round.error",
                            status="error",
                            metadata={
                                "error_class": type(exc).__name__,
                                "observable_outcome": "llm_error",
                                "guard_results": state.guard_results[guard_results_start:],
                            },
                            payload={"error": str(exc)},
                        )
                    result = harness_llm_error(config, state, round_num)
                    _emit_checkpoint(
                        config,
                        state,
                        phase="llm_error",
                        round_index=round_index,
                        exit_reason=result.exit_reason,
                    )
                    return result

                state.messages.append(normalize_response(response))
                tool_calls = response.get("tool_calls")
                tool_call_count = len(tool_calls or [])
                round_span.set_attribute("agent.tool_call_count", tool_call_count)
                round_span.set_attribute("agent.output_chars", len(str(response.get("content") or "")))

                if not tool_calls:
                    if harness_tool_nudge(config, state, response.get("content") or ""):
                        round_span.set_attribute("agent.tool_nudge", True)
                        _set_tool_selection_span_attributes(round_span, selection_funnel)
                        if context is not None:
                            emit_event(
                                context,
                                "round.end",
                                metadata={
                                    "tool_call_count": 0,
                                    "observable_outcome": "tool_nudge",
                                    "message_count": len(state.messages),
                                    "guard_results": state.guard_results[guard_results_start:],
                                    "tool_selection_funnel": selection_funnel,
                                },
                                payload={"assistant_response": response},
                            )
                        continue
                    result = harness_finalize(config, state, response, round_num)
                    _set_tool_selection_span_attributes(round_span, selection_funnel)
                    if context is not None:
                        emit_event(
                            context,
                            "round.end",
                            metadata={
                                "tool_call_count": 0,
                                "observable_outcome": "final_response",
                                "exit_reason": result.exit_reason,
                                "message_count": len(state.messages),
                                "guard_results": state.guard_results[guard_results_start:],
                                "tool_selection_funnel": selection_funnel,
                            },
                            payload={"assistant_response": response},
                        )
                    _emit_checkpoint(
                        config,
                        state,
                        phase="final",
                        round_index=round_index,
                        exit_reason=result.exit_reason,
                    )
                    return result

                if config.on_stream_delta is not None:
                    config.on_stream_delta(None)

                collect_think_reasoning(state, response, tool_calls)
                try:
                    selection_funnel = harness_execute_tools(
                        config,
                        state,
                        tool_calls,
                        round_num,
                        max_rounds=max_rounds,
                        budget_warning_round=budget_warning_round,
                    )
                    _set_tool_selection_span_attributes(round_span, selection_funnel)
                except Exception as exc:
                    if (
                        config.deadline_monotonic is not None
                        and time.monotonic() >= config.deadline_monotonic
                    ):
                        return _deadline_loop_result(config, state, round_num + 1)
                    if context is not None:
                        emit_event(
                            context,
                            "round.error",
                            status="error",
                            metadata={
                                "error_class": type(exc).__name__,
                                "observable_outcome": "tool_execution_error",
                                "tool_call_count": tool_call_count,
                                "guard_results": state.guard_results[guard_results_start:],
                            },
                            payload={"error": str(exc), "assistant_response": response},
                        )
                    raise
                if context is not None:
                    emit_event(
                        context,
                        "round.end",
                        metadata={
                            "tool_call_count": tool_call_count,
                            "tool_names": [
                                str(call.get("function", {}).get("name") or "")
                                for call in tool_calls
                            ],
                            "observable_outcome": "tool_execution",
                            "message_count": len(state.messages),
                            "guard_results": state.guard_results[guard_results_start:],
                            "tool_selection_funnel": selection_funnel,
                        },
                        payload={"assistant_response": response},
                    )
                _emit_checkpoint(
                    config,
                    state,
                    phase="tool_round_complete",
                    round_index=round_index,
                )

    result = harness_exhaustion(config, state, max_rounds)
    _emit_checkpoint(
        config,
        state,
        phase="exhausted",
        round_index=max_rounds,
        exit_reason=result.exit_reason,
    )
    return result
