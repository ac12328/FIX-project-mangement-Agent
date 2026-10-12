"""Harness layers around the golden agent loop (pre/finalize/execute/exhaustion)."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _HKT_TZ = _ZoneInfo("Asia/Hong_Kong")
except Exception:
    _HKT_TZ = timezone.utc  # type: ignore[assignment]

from src.agent.anchors import anchor_system_message, extract_anchors
from src.agent.claims import format_claim_violation_reply, verify_claims
from src.agent.exhaustion import build_exhaustion_reply
from src.agent.email_analysis_schema_repair import (
    EmailAnalysisSchemaRepairState,
    append_schema_repair_nudge,
    prepare_email_analysis_tool_calls,
)
from src.agent.loop_config import LoopConfig, LoopResult
from src.agent.meta_messages import upsert_meta_message
from src.agent.round_context import wrap_pre_round_hook
from src.agent.safety import (
    extract_fallback_text,
    extract_reasoning,
    normalize_assistant_message,
    sanitize_for_user,
)
from src.agent.stall import detect_stall
from src.agent.state_capsule import LoopStateCapsule, build_loop_state_capsule
from src.agent.tool_receipts import ToolExecutionReceipt
from src.agent.tool_result_cache import (
    CachedToolResult,
    is_cacheable_tool,
    remember_successful_result,
    replay_cached_result,
)
from src.llm.client import llm_call_with_tools
from src.llm.tool_stream import llm_call_with_tools_stream
logger = logging.getLogger(__name__)

_WRITE_TOOLS = frozenset({
    "update_project", "update_project_batch",
    "create_project", "confirm_create_project", "cancel_create_project",
    "add_note", "manage_followup", "add_contact",
    "confirm_update", "log_evidence",
    "draft_reply", "edit_draft", "discard_draft",
    "wiki_create", "wiki_update", "wiki_link",
})


@dataclass
class LoopState:
    messages: list[dict[str, Any]]
    tool_history: list[tuple[str, ...]] = field(default_factory=list)
    think_reasoning_parts: list[str] = field(default_factory=list)
    last_tool_result: str | None = None
    stall_count: int = 0
    force_respond: bool = False
    exit_reason: str = "model_stop"
    tool_nudge_count: int = 0
    original_user_text: str = ""
    loop_start_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    round_context: Any = None
    execution_receipts: list[ToolExecutionReceipt] = field(default_factory=list)
    guard_results: list[dict[str, Any]] = field(default_factory=list)
    email_analysis_schema_repair: EmailAnalysisSchemaRepairState = field(
        default_factory=EmailAnalysisSchemaRepairState,
    )
    state_capsule: LoopStateCapsule | None = None
    tool_call_signatures: set[str] = field(default_factory=set)
    duplicate_tool_selection_count: int = 0
    tool_result_cache: dict[str, CachedToolResult] = field(default_factory=dict)


_META_USER_PREFIXES = (
    "<context-summary>",
    "<deterministic-context-compression",
    "<identity>",
    "<loop-anchors",
    "<loop-state-capsule",
    "<planning-hint>",
    "<project-scope",
    "<reminder>",
    "<round-context>",
    "<stall-escalation",
    "<system-state",
    "<tool-requirement>",
)


def is_operator_user_content(content: object) -> bool:
    if not isinstance(content, str) or not content.strip():
        return False
    return not content.strip().startswith(_META_USER_PREFIXES)


def extract_original_user_text(messages: list[dict[str, Any]]) -> str:
    return next(
        (
            m.get("content") or ""
            for m in reversed(messages)
            if m.get("role") == "user" and is_operator_user_content(m.get("content"))
        ),
        "",
    )


def init_loop_state(config: LoopConfig) -> LoopState:
    loop_start = config.loop_start_utc or datetime.now(timezone.utc)
    round_context = config.round_context_provider
    if round_context is None and config.pre_round_hook is not None:
        round_context = wrap_pre_round_hook(config.pre_round_hook)

    from src.tools.health_tracker import get_tracker

    get_tracker().record_required(config.required_tools_before_reply or ())
    return LoopState(
        messages=list(config.messages),
        original_user_text=extract_original_user_text(config.messages),
        loop_start_utc=loop_start,
        round_context=round_context,
    )


def try_staged_confirm_shortcut(config: LoopConfig, state: LoopState) -> LoopResult | None:
    # Only an operator-facing Telegram turn may deterministically confirm/cancel
    # an existing pending operation. Specialist tasks often contain instructions
    # such as "stage, then call confirm_update"; treating that prose as an
    # operator confirmation skips staging and terminates the specialist early.
    if config.surface != "chat" or (config.caller_name or "").startswith("specialist:"):
        return None

    from src.agent.guards import (
        try_deterministic_staged_cancel,
        try_deterministic_staged_confirm,
    )

    staged_reply = try_deterministic_staged_confirm(
        config.conn,
        state.original_user_text,
        chat_id=config.chat_id,
        operator_id=config.operator_id,
    )
    tool_name = "confirm_update"
    exit_reason = "deterministic_staged_confirm"
    if not staged_reply:
        staged_reply = try_deterministic_staged_cancel(
            config.conn,
            state.original_user_text,
            chat_id=config.chat_id,
            operator_id=config.operator_id,
        )
        tool_name = "cancel_update"
        exit_reason = "deterministic_staged_cancel"
    if not staged_reply:
        return None
    return LoopResult(
        reply=staged_reply,
        messages=state.messages,
        round_count=1,
        tool_history=[(tool_name,)],
        exit_reason=exit_reason,
    )


def invoke_llm_round(
    config: LoopConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict:
    from src.llm.watchdog import LLM_REASONING_TIMEOUT_SEC

    if tools:
        return llm_call_with_tools(
            "reasoning", messages, tools, temperature=config.temperature,
            timeout_sec=LLM_REASONING_TIMEOUT_SEC,
        )
    if config.on_stream_delta is None:
        return llm_call_with_tools(
            "reasoning", messages, tools, temperature=config.temperature,
            timeout_sec=LLM_REASONING_TIMEOUT_SEC,
        )
    from src.llm.client import _cap_max_tokens, _trim_messages_to_fit

    content_cb = config.on_stream_delta

    def _on_content_delta(text: str) -> None:
        if content_cb is not None:
            content_cb(text)

    try:
        return llm_call_with_tools_stream(
            "reasoning",
            messages,
            tools,
            on_delta=_on_content_delta,
            temperature=config.temperature,
            timeout_sec=LLM_REASONING_TIMEOUT_SEC,
            trim_fn=_trim_messages_to_fit,
            cap_tokens_fn=_cap_max_tokens,
        )
    except Exception:
        logger.warning(
            "%s: streaming LLM call failed — falling back to non-streaming",
            config.caller_name,
            exc_info=True,
        )
        return llm_call_with_tools(
            "reasoning", messages, tools, temperature=config.temperature,
            timeout_sec=LLM_REASONING_TIMEOUT_SEC,
        )


def looks_like_tool_error(text: str) -> bool:
    lower = (text or "").lower()
    return (
        "invalid tool arguments" in lower
        or lower.startswith('{"error"')
        or lower.startswith("error:")
    )


def harness_pre_process(
    config: LoopConfig,
    state: LoopState,
    round_num: int,
    *,
    max_rounds: int,
    budget_warning_round: int,
) -> None:
    messages = state.messages
    if round_num > 0 and config.enable_compression:
        from src.agent.core_loop import micro_compact_tool_results
        from src.context.compression import compress_deterministically

        messages = micro_compact_tool_results(messages, recent_turns=3)
        messages = compress_deterministically(
            messages,
            protected_user_contents=(state.original_user_text,),
        )

    state.state_capsule = build_loop_state_capsule(
        config,
        state,
        rounds_completed=round_num,
        max_rounds=max_rounds,
    )
    messages = upsert_meta_message(
        messages,
        state.state_capsule.to_message(),
        marker="<loop-state-capsule",
    )

    # Harness: re-assert project scope every round (any project, not case-specific)
    if config.effective_project_id:
        from src.agent.project_scope import ProjectScope, project_scope_system_message

        scope_msg = project_scope_system_message(
            ProjectScope(
                effective_project_id=config.effective_project_id,
                explicit_from_message=bool(
                    getattr(config, "project_scope_forced", False)
                ),
                source="explicit" if getattr(config, "project_scope_forced", False) else "active",
            ),
            state.original_user_text,
        )
        if scope_msg:
            messages = upsert_meta_message(
                messages, scope_msg, marker="<project-scope",
            )

    if config.refresh_anchors_each_round and config.effective_project_id:
        anchors = extract_anchors(config.conn, effective_project_id=config.effective_project_id)
        messages = upsert_meta_message(
            messages, anchor_system_message(anchors), marker="<loop-anchors",
        )

    if state.round_context is not None:
        ctx_body = state.round_context.poll(
            round_num=round_num,
            conn=config.conn,
            effective_project_id=config.effective_project_id,
            loop_start_utc=state.loop_start_utc,
        )
        if ctx_body:
            messages = upsert_meta_message(
                messages,
                {"role": "user", "content": f"<round-context>\n{ctx_body}\n</round-context>"},
                marker="<round-context>",
            )

    _now = datetime.now(_HKT_TZ)
    _time_str = _now.strftime("%Y-%m-%dT%H:%M+08:00")
    messages = upsert_meta_message(
        messages,
        {
            "role": "user",
            "content": f'<system-state time="{_time_str}" round="{round_num + 1}/{max_rounds}" />',
        },
        marker="<system-state",
    )

    if state.force_respond:
        messages = upsert_meta_message(
            messages,
            {
                "role": "user",
                "content": (
                    "<stall-escalation>You must respond with a final answer now. "
                    "No more tool calls.</stall-escalation>"
                ),
            },
            marker="<stall-escalation>",
        )

    state.messages = messages


def harness_tool_nudge(
    config: LoopConfig,
    state: LoopState,
    content: str,
) -> bool:
    """Inject required-tool or bounded schema-repair nudge."""
    if append_schema_repair_nudge(
        state.email_analysis_schema_repair,
        state.messages,
        force_respond=state.force_respond,
    ):
        return True
    if not config.required_tools_before_reply or state.force_respond:
        return False
    called = {name for rnd in state.tool_history for name in rnd}
    missing = set(config.required_tools_before_reply) - called
    if not missing or state.tool_nudge_count >= config.max_tool_nudge_rounds:
        return False
    state.tool_nudge_count += 1
    state.messages.append({
        "role": "user",
        "content": (
            "<tool-requirement>"
            f"Call required tools before your final answer: "
            f"{', '.join(sorted(missing))}. "
            "Use wiki_search for domain facts; do not answer from prompt memory."
            "</tool-requirement>"
        ),
    })
    return True


def harness_finalize(
    config: LoopConfig,
    state: LoopState,
    response: dict[str, Any],
    round_num: int,
) -> LoopResult:
    content = response.get("content") or ""
    from src.agent.guards import try_auto_confirm_evidence_milestone

    read_only_execution = bool(config.execution_context.get("read_only_execution"))
    if (
        not read_only_execution
        and try_auto_confirm_evidence_milestone(
            config.conn,
            state.original_user_text,
            state.tool_history,
        )
    ):
        state.tool_history.append(("confirm_update",))

    # Read-only execution blocks mutation tools, but it must not exempt the
    # final response from mutation-claim validation. Otherwise a model could
    # claim that a write succeeded without any supporting receipt.
    violations = verify_claims(
        content,
        tool_history=state.tool_history,
        conn=config.conn,
        effective_project_id=config.effective_project_id,
        execution_receipts=state.execution_receipts,
    )
    if violations and state.original_user_text:
        from src.agent.guards import is_draft_request

        if is_draft_request(state.original_user_text):
            violations = [v for v in violations if v.code != "draft_unverified"]
    guard_enabled = bool(config.enable_fabrication_guard)
    if violations and guard_enabled:
        guard_outcome = "blocked"
    elif violations:
        guard_outcome = "observed_not_enforced"
    elif guard_enabled:
        guard_outcome = "passed"
    else:
        guard_outcome = "disabled"
    state.guard_results.append({
        "guard": "final_claim_validation",
        "outcome": guard_outcome,
        "enabled": guard_enabled,
        "violation_count": len(violations),
        "violation_codes": [violation.code for violation in violations],
    })

    exit_reason = state.exit_reason
    if violations and config.enable_fabrication_guard:
        result = format_claim_violation_reply(violations)
        exit_reason = "claim_violation"
    elif config.enable_fabrication_guard:
        write_tools_called = any(
            name in _WRITE_TOOLS
            for round_tools in state.tool_history
            for name in round_tools
        )
        result = sanitize_for_user(
            content,
            tools_were_called=write_tools_called or read_only_execution,
            show_reasoning=config.show_thinking_window,
        )
    else:
        result = content

    if not result:
        result = "唔確定點幫你處理呢個請求，請重試或使用 /help。"
        exit_reason = "no_answer"

    if (
        config.show_thinking_window
        and state.think_reasoning_parts
        and not result.startswith("\U0001f4ad")
    ):
        from src.agent.safety import format_reasoning_blockquote
        merged = "\n".join(state.think_reasoning_parts[-2:])
        if len(merged) > 200:
            merged = merged[:200] + "..."
        result = format_reasoning_blockquote(merged) + "\n\n" + result

    return LoopResult(
        reply=result,
        messages=state.messages,
        round_count=round_num + 1,
        tool_history=state.tool_history,
        exit_reason=exit_reason,
        answer_streamed=bool(
            config.on_stream_delta is not None
            and config.stream_flags.get("final_answer_streamed")
        ),
        execution_receipts=state.execution_receipts,
        guard_results=list(state.guard_results),
    )


def collect_think_reasoning(state: LoopState, response: dict[str, Any], tool_calls: list) -> None:
    intermediate_content = response.get("content") or ""
    if intermediate_content:
        intermediate_reasoning = extract_reasoning(intermediate_content)
        if intermediate_reasoning:
            state.think_reasoning_parts.append(intermediate_reasoning)
    for tc in tool_calls:
        if tc["function"]["name"] == "think":
            try:
                raw_args = tc["function"].get("arguments") or "{}"
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                reasoning_text = args.get("reasoning", "")
                if reasoning_text:
                    state.think_reasoning_parts.append(reasoning_text)
            except (json.JSONDecodeError, TypeError):
                pass


def _tool_call_signature(tool_call: dict[str, Any]) -> str:
    """Return a privacy-safe signature for semantically identical tool calls."""
    function_data = tool_call.get("function", {})
    raw_arguments = function_data.get("arguments") or {}
    try:
        arguments = (
            json.loads(raw_arguments)
            if isinstance(raw_arguments, str)
            else raw_arguments
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        arguments = str(raw_arguments)
    canonical = json.dumps(
        {"name": function_data.get("name", ""), "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_pre_execution_receipt(
    state: LoopState,
    tool_call: dict[str, Any],
    *,
    outcome: str,
    error_code: str,
    status: str = "denied",
) -> None:
    """Publish an authoritative receipt for a call that never executed."""
    function_data = tool_call.get("function", {})
    tool_name = str(function_data.get("name") or "")
    receipt = ToolExecutionReceipt(
        tool_call_id=str(tool_call.get("id") or ""),
        tool_name=tool_name,
        status=status,
        outcome=outcome,
        project_id=None,
        error_code=error_code,
        execution_started=False,
    )
    state.execution_receipts.append(receipt)

    from src.tools.health_tracker import get_tracker

    get_tracker().record_outcome(
        tool_name,
        status=receipt.status,
        error_code=receipt.error_code,
    )


def harness_execute_tools(
    config: LoopConfig,
    state: LoopState,
    tool_calls: list[dict],
    round_num: int,
    *,
    max_rounds: int,
    budget_warning_round: int,
) -> dict[str, int]:
    from src.tools.health_tracker import get_tracker

    tracker = get_tracker()
    receipts_start = len(state.execution_receipts)
    duplicate_selection_count = 0
    blocked = config.blocked_tool_names
    advertised = frozenset(
        tool.get("function", {}).get("name", "")
        for tool in config.tools
        if tool.get("function", {}).get("name")
    )
    runtime_allowed = (
        advertised
        if config.runtime_allowed_tool_names is None
        else advertised.intersection(config.runtime_allowed_tool_names)
    )
    allowed_calls: list[dict] = []
    blocked_messages: list[dict] = []
    for tc in tool_calls:
        name = tc["function"]["name"]
        signature = _tool_call_signature(tc)
        repeated = signature in state.tool_call_signatures
        state.tool_call_signatures.add(signature)
        if repeated:
            duplicate_selection_count += 1
            state.duplicate_tool_selection_count += 1
        tracker.record_selection(name, repeated=repeated)

        guard_result = {
            "guard": "tool_authorization",
            "tool_call_id": tc.get("id"),
            "tool_name": name,
            "outcome": "allowed",
            "reason": "advertised_and_permitted",
        }
        if name not in runtime_allowed:
            reason = "tool_not_allowed"
            guard_result.update({"outcome": "blocked", "reason": reason})
            state.guard_results.append(guard_result)
            tracker.record_authorization(name, allowed=False, reason=reason)
            blocked_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": name,
                "content": json.dumps({
                    "ok": False,
                    "status": "denied",
                    "error": reason,
                    "error_code": reason,
                    "tool": name,
                    "message": f"Tool '{name}' is not available to this agent role.",
                }),
            })
            _record_pre_execution_receipt(
                state, tc, outcome=reason, error_code=reason,
            )
            continue
        if name in blocked:
            reason = "explicit_project_scope_required"
            guard_result.update({"outcome": "blocked", "reason": reason})
            state.guard_results.append(guard_result)
            tracker.record_authorization(name, allowed=False, reason=reason)
            blocked_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": name,
                "content": json.dumps({
                    "ok": False,
                    "status": "denied",
                    "error": reason,
                    "error_code": reason,
                    "tool": name,
                    "message": (
                        f"Tool '{name}' is blocked. The operator must provide an "
                        "explicit project ID in this message before staging or "
                        "confirming updates."
                    ),
                }),
            })
            _record_pre_execution_receipt(
                state, tc, outcome=reason, error_code=reason,
            )
            continue
        state.guard_results.append(guard_result)
        tracker.record_authorization(name, allowed=True)
        allowed_calls.append(tc)

    initially_authorized_count = len(allowed_calls)
    tool_names = [tc["function"]["name"] for tc in allowed_calls]
    if blocked_messages:
        logger.info(
            "%s: round %d blocked tools = %s",
            config.caller_name,
            round_num + 1,
            [
                tc["function"]["name"]
                for tc in tool_calls
                if tc["function"]["name"] not in tool_names
            ],
        )
    logger.info(
        "%s: round %d tool calls = %s",
        config.caller_name,
        round_num + 1,
        tool_names,
    )

    from src.agent.operator_identity import resolve_operator_id

    if config.effective_project_id:
        from src.agent.project_scope import inject_project_id_args

        corrected_calls: list[dict[str, Any]] = []
        for tool_call in allowed_calls:
            function_data = dict(tool_call.get("function", {}))
            raw_args = function_data.get("arguments") or "{}"
            try:
                arguments = (
                    json.loads(raw_args)
                    if isinstance(raw_args, str)
                    else dict(raw_args)
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                corrected_calls.append(tool_call)
                continue
            corrected_args = inject_project_id_args(
                function_data.get("name", ""),
                arguments,
                forced_project_id=config.effective_project_id,
                override_explicit=bool(
                    getattr(config, "project_scope_typo_corrected", False)
                ),
            )
            function_data["arguments"] = json.dumps(
                corrected_args, ensure_ascii=False,
            )
            corrected_call = dict(tool_call)
            corrected_call["function"] = function_data
            corrected_calls.append(corrected_call)
        allowed_calls = corrected_calls

    allowed_calls, schema_messages, schema_guards = prepare_email_analysis_tool_calls(
        state.email_analysis_schema_repair, allowed_calls,
    )
    blocked_messages.extend(schema_messages)
    state.guard_results.extend(schema_guards)
    calls_by_id = {str(call.get("id") or ""): call for call in tool_calls}
    for guard in schema_guards:
        if guard.get("outcome") != "blocked":
            continue
        tool_call = calls_by_id.get(str(guard.get("tool_call_id") or ""))
        if tool_call is None:
            continue
        reason = str(guard.get("reason") or "tool_arguments_schema_invalid")
        name = str(guard.get("tool_name") or "report_email_analysis")
        tracker.record_pre_execution_block(name, reason=reason)
        _record_pre_execution_receipt(
            state,
            tool_call,
            outcome=reason,
            error_code="tool_arguments_schema_invalid",
            status="failed",
        )
    tool_names = [tc["function"]["name"] for tc in allowed_calls]
    ordered_allowed_calls = list(allowed_calls)
    cached_messages: dict[str, dict[str, Any]] = {}
    calls_to_execute: list[dict[str, Any]] = []
    for tool_call in ordered_allowed_calls:
        tool_call_id = str(tool_call.get("id") or "")
        tool_name = str(tool_call.get("function", {}).get("name") or "")
        signature = _tool_call_signature(tool_call)
        cached = state.tool_result_cache.get(signature)
        if cached is None or not is_cacheable_tool(tool_name):
            calls_to_execute.append(tool_call)
            continue
        cached_message, cached_receipt = replay_cached_result(cached, tool_call)
        cached_messages[tool_call_id] = cached_message
        state.execution_receipts.append(cached_receipt)
        from src.agent.control_plane_metrics import control_plane_metrics

        control_plane_metrics.increment(
            "duplicate_read_replay", outcome="replayed"
        )
        state.guard_results.append({
            "guard": "tool_result_cache",
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "outcome": "replayed",
            "reason": "identical_successful_read",
        })

    guard_op_id = config.operator_id or resolve_operator_id(config.conn)
    from src.agent.core_loop import execute_tool_calls

    fresh_receipts_start = len(state.execution_receipts)
    tool_phase_deadline = config.tool_phase_deadline()
    tool_messages: list[dict[str, Any]] = []
    # Preserve the legacy blocked-call boundary (an empty executor batch can
    # still be observed by guards/tests), but skip the executor when every
    # authorized call was satisfied by the per-loop read cache.
    if calls_to_execute or not cached_messages:
        tool_messages = execute_tool_calls(
            config.conn, config.intent, calls_to_execute,
            progress_cb=config.progress_cb,
            operator_id=guard_op_id,
            digest_tool_results=config.digest_tool_results,
            thread_id=config.thread_id,
            project_id=config.effective_project_id,
            chat_id=config.chat_id,
            allowed_tool_names=runtime_allowed,
            blocked_tool_names=blocked,
            receipt_sink=state.execution_receipts.append,
            execution_context={
                **config.execution_context,
                "operator_request_text": (
                    config.operator_request_text
                    or config.execution_context.get("operator_request_text")
                ),
                "explicit_operator_instruction": config.explicit_operator_instruction,
                "deterministic_authorization": config.deterministic_authorization,
                "write_connection_factory": config.write_connection_factory,
                "selection_source": "model",
                **(
                    {"deadline_monotonic": tool_phase_deadline}
                    if tool_phase_deadline is not None
                    else {}
                ),
            },
        )
    fresh_receipts = state.execution_receipts[fresh_receipts_start:]
    fresh_receipts_by_id = {
        receipt.tool_call_id: receipt for receipt in fresh_receipts
    }
    fresh_messages_by_id = {
        str(message.get("tool_call_id") or ""): message
        for message in tool_messages
    }
    for tool_call in calls_to_execute:
        tool_call_id = str(tool_call.get("id") or "")
        message = fresh_messages_by_id.get(tool_call_id)
        receipt = fresh_receipts_by_id.get(tool_call_id)
        if message is None or receipt is None:
            continue
        remember_successful_result(
            state.tool_result_cache,
            signature=_tool_call_signature(tool_call),
            tool_call=tool_call,
            message=message,
            receipt=receipt,
        )

    ordered_tool_messages: list[dict[str, Any]] = []
    consumed_message_ids: set[str] = set()
    for tool_call in ordered_allowed_calls:
        tool_call_id = str(tool_call.get("id") or "")
        message = cached_messages.get(tool_call_id) or fresh_messages_by_id.get(tool_call_id)
        if message is not None:
            ordered_tool_messages.append(message)
            consumed_message_ids.add(tool_call_id)
    ordered_tool_messages.extend(
        message
        for message in tool_messages
        if str(message.get("tool_call_id") or "") not in consumed_message_ids
    )

    if config.thread_artifacts_enabled and config.conn and config.thread_id:
        from src.memory.thread_artifacts import maybe_offload
        op_id = config.operator_id or resolve_operator_id(config.conn)
        for msg in ordered_tool_messages:
            content = msg.get("content") or ""
            if isinstance(content, str) and len(content) > 4000:
                msg["content"] = maybe_offload(
                    config.conn,
                    operator_id=op_id,
                    thread_id=config.thread_id,
                    tool_name=msg.get("name", "tool"),
                    content=content,
                    project_id=config.effective_project_id,
                )
    tool_messages = blocked_messages + ordered_tool_messages
    state.messages.extend(tool_messages)
    state.last_tool_result = tool_messages[-1]["content"] if tool_messages else None
    state.tool_history.append(tuple(tool_names))

    if round_num + 1 >= budget_warning_round:
        state.messages = upsert_meta_message(
            state.messages,
            {
                "role": "user",
                "content": (
                    f"<reminder>You have used {round_num + 1} of {max_rounds} tool rounds. "
                    "Wrap up and respond to the operator soon.</reminder>"
                ),
            },
            marker="<reminder>You have used",
        )

    stall_signal = detect_stall(state.tool_history, state.messages)
    if stall_signal is not None:
        state.stall_count += 1
        state.messages = upsert_meta_message(
            state.messages,
            {
                "role": "user",
                "content": (
                    f"<reminder>Stall detected ({stall_signal.kind}): {stall_signal.detail}. "
                    "Try a different approach or respond with what you have.</reminder>"
                ),
            },
            marker="<reminder>Stall detected",
        )
        if state.stall_count >= 2:
            state.force_respond = True
            state.exit_reason = "stall_force"

    from src.agent.evidence_sufficiency import (
        canonical_wiki_evidence,
        evidence_sufficient_message,
        should_stop_after_canonical_wiki,
    )

    if should_stop_after_canonical_wiki(
        specialist_type=config.execution_context.get("specialist_type"),
        user_text=state.original_user_text,
        receipts=state.execution_receipts,
    ):
        canonical = canonical_wiki_evidence(state.execution_receipts)
        locator = canonical[0][0]
        state.force_respond = True
        state.exit_reason = "evidence_sufficient"
        from src.agent.control_plane_metrics import control_plane_metrics

        control_plane_metrics.increment(
            "canonical_evidence_stop", outcome="forced"
        )
        state.messages = upsert_meta_message(
            state.messages,
            evidence_sufficient_message(locator),
            marker="<evidence-sufficiency",
        )
        state.guard_results.append({
            "guard": "evidence_sufficiency",
            "outcome": "force_final_response",
            "source": locator,
            "authority": "canonical",
        })

    round_receipts = state.execution_receipts[receipts_start:]
    executed_count = sum(receipt.execution_started for receipt in round_receipts)
    return {
        "selected": len(tool_calls),
        "authorized": initially_authorized_count,
        "blocked": max(0, len(tool_calls) - executed_count),
        "executed": executed_count,
        "succeeded": sum(receipt.status == "succeeded" for receipt in round_receipts),
        "failed": sum(receipt.status == "failed" for receipt in round_receipts),
        "denied": sum(receipt.status == "denied" for receipt in round_receipts),
        "duplicate_selection": duplicate_selection_count,
    }


def harness_llm_error(config: LoopConfig, state: LoopState, round_num: int) -> LoopResult:
    if state.last_tool_result:
        result = extract_fallback_text(state.last_tool_result) or str(state.last_tool_result)
        if looks_like_tool_error(result):
            result = "處理請求時 LLM 發生錯誤，請重試並提供要處理的專案識別碼。"
        return LoopResult(
            reply=result,
            messages=state.messages,
            round_count=round_num + 1,
            tool_history=state.tool_history,
            exit_reason="llm_error",
            execution_receipts=state.execution_receipts,
            guard_results=list(state.guard_results),
        )
    return LoopResult(
        reply="處理請求時發生錯誤，請稍後重試或使用 /status、/query。",
        messages=state.messages,
        round_count=round_num + 1,
        tool_history=state.tool_history,
        exit_reason="llm_error",
        execution_receipts=state.execution_receipts,
        guard_results=list(state.guard_results),
    )


def harness_exhaustion(config: LoopConfig, state: LoopState, max_rounds: int) -> LoopResult:
    logger.warning("%s: exhausted %d rounds without model exit", config.caller_name, max_rounds)
    result = build_exhaustion_reply(
        state.messages,
        state.tool_history,
        max_rounds=max_rounds,
        execution_receipts=state.execution_receipts,
    )
    from src.agent.core_loop import summarise_on_exhaustion
    summary = summarise_on_exhaustion(state.messages)
    if summary:
        result = result + "\n\n" + summary[:500]
    elif not result.strip():
        result = extract_fallback_text(state.last_tool_result) or (
            "已達處理上限，請縮小範圍或分拆請求後重試。"
        )
    return LoopResult(
        reply=result,
        messages=state.messages,
        round_count=max_rounds,
        tool_history=state.tool_history,
        exit_reason="exhausted",
        execution_receipts=state.execution_receipts,
        guard_results=list(state.guard_results),
    )


def normalize_response(response: dict[str, Any]) -> dict[str, Any]:
    return normalize_assistant_message(response)
