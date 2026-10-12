"""Single entry point for unified loop v2 surfaces."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from src.agent.core_loop import run_agent_loop
from src.agent.loop_config import InferenceSurface, LoopConfig
from src.agent.memory_loop_helpers import context_operator_id, loop_memory_kwargs
from src.agent.post_loop import apply_shared_post_loop
from src.agent.prepare_loop import prepare_loop
from src.agent.round_context import (
    CompositeRoundContext,
    DbDriftProvider,
    TelegramNotificationsProvider,
)
from src.observability.progress import ProgressCallback
from src.observability.contracts import TraceContext, trace_context_scope
from src.observability.lifecycle import (
    context_composition_metadata,
    emit_event,
    emit_run_finish,
    emit_run_start,
    tool_contract_metadata,
)
from src.events.notification_queue import drain_notifications
from src.tools.role_tools import AgentRole, get_tools_for_role


@dataclass
class HarnessProfile:
    digest_tool_results: bool = False
    context_profile: str = "full"


@dataclass
class LoopRequest:
    surface: InferenceSurface
    user_text: str
    role: AgentRole
    chat_id: int | None = None
    project_id: str | None = None
    harness: HarnessProfile | None = None
    max_rounds: int | None = None
    progress_cb: ProgressCallback | None = None
    on_stream_delta: Callable[[str | None], None] | None = None
    required_tools_before_reply: frozenset[str] | None = None
    explicit_operator_instruction: bool = False
    operator_request_text: str | None = None
    exposure_scopes: tuple[Any, ...] = ()
    operator_id: str | None = None
    thread_id: str | None = None
    execution_context: dict[str, Any] | None = None
    enable_fabrication_guard: bool = True
    apply_shared_post_processing: bool = True
    trace_context: TraceContext | None = None
    preparation_version: str | None = None
    preparation_snapshot: dict[str, Any] | None = None
    pre_loop_trace_events: tuple[dict[str, Any], ...] = ()
    trace_finish_owned_by_caller: bool = False


def _role_context_profile(role: AgentRole) -> str:
    if role == AgentRole.HOST:
        return "bootstrap"
    if role == AgentRole.QUERY:
        return "minimal"
    return "full"


def resolve_role_for_intent(intent: str) -> AgentRole:
    mapping = {
        "orchestrator": AgentRole.HOST,
        "query": AgentRole.QUERY,
        "email": AgentRole.EMAIL,
        "casual": AgentRole.HOST,
        "general": AgentRole.GENERAL,
    }
    return mapping.get(intent, AgentRole.GENERAL)


def resolve_role_for_v2(intent_label: str) -> AgentRole:
    """Unified v2: orchestrator host always uses HOST role; intent is label-only."""
    from config import defaults

    if getattr(defaults, "ORCHESTRATOR_MODE", False):
        return AgentRole.HOST
    return resolve_role_for_intent(intent_label)


def _trusted_explicit_operator_instruction(user_text: str) -> bool:
    from src.agent.guards import requests_explicit_milestone_write
    return requests_explicit_milestone_write(user_text)


def build_telegram_request(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    *,
    intent: str,
    effective_project_id: str | None,
    max_rounds: int | None = None,
    progress_cb: ProgressCallback | None = None,
    on_stream_delta: Callable[[str | None], None] | None = None,
    required_tools_before_reply: frozenset[str] | None = None,
) -> LoopRequest:
    role = resolve_role_for_intent(intent)
    return LoopRequest(
        surface="chat",
        user_text=user_text,
        role=role,
        chat_id=chat_id,
        project_id=effective_project_id,
        harness=HarnessProfile(
            digest_tool_results=(role == AgentRole.HOST),
            context_profile=_role_context_profile(role),
        ),
        max_rounds=max_rounds,
        progress_cb=progress_cb,
        on_stream_delta=on_stream_delta,
        required_tools_before_reply=required_tools_before_reply,
        explicit_operator_instruction=_trusted_explicit_operator_instruction(user_text),
    )


def _emit_trace_best_effort(emitter: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Keep synchronous trace sink failures out of the business path."""
    try:
        emitter(*args, **kwargs)
    except Exception:
        pass


def run_surface_loop(
    conn: sqlite3.Connection | None,
    request: LoopRequest,
    *,
    messages: list[dict[str, Any]],
    context_frame: Any,
    tools: list[dict[str, Any]] | None = None,
    intent: str | None = None,
    extra_messages: list[dict[str, Any]] | None = None,
    blocked_tool_names: frozenset[str] | None = None,
    max_rounds: int = 8,
    show_thinking: bool = False,
) -> tuple[str, Any, Any]:
    """Run core loop plus shared post-loop under one engineering trace."""
    if tools is None:
        tools = get_tools_for_role(request.role)
    msgs = list(messages)
    if extra_messages:
        msgs.extend(extra_messages)

    intent_label = intent or request.role.value
    context = request.trace_context or TraceContext.create(
        surface=str(request.surface),
        task=intent_label,
        thread_id=request.thread_id,
    )
    trace_task = context.task
    request.trace_context = context
    tool_names = [
        str(tool.get("function", {}).get("name") or "")
        for tool in tools
        if tool.get("function", {}).get("name")
    ]

    with trace_context_scope(context):
        _emit_trace_best_effort(
            emit_run_start,
            context,
            metadata={
                "surface": str(request.surface),
                "task": trace_task,
                "project_id": request.project_id,
                "session_id": request.thread_id,
                "chat_id": request.chat_id,
                "operator_id": request.operator_id,
                "intent": intent_label,
                "max_rounds": max_rounds,
                "tool_names": tool_names,
                "preparation_version": request.preparation_version,
            },
            payload={"user_input": request.user_text},
        )
        _emit_trace_best_effort(
            emit_event,
            context,
            "context.composed",
            metadata={
                **context_composition_metadata(
                    msgs,
                    profile=request.harness.context_profile if request.harness else None,
                    context_frame=context_frame,
                ),
                **tool_contract_metadata(tools),
                "tool_names": tool_names,
                "preparation_version": request.preparation_version,
                "intent": intent_label,
            },
            payload={"ordered_messages": msgs},
        )
        for pending_event in request.pre_loop_trace_events:
            try:
                event_data = dict(pending_event)
                event_type = str(event_data.pop("event_type", "")).strip()
                if event_type:
                    emit_event(context, event_type, **event_data)
            except Exception:
                # Shadow observability must never affect loop execution.
                continue
        if request.preparation_version or request.preparation_snapshot:
            _emit_trace_best_effort(
                emit_event,
                context,
                "prepare.snapshot",
                metadata={
                    "preparation_version": request.preparation_version,
                    **(request.preparation_snapshot or {}),
                },
            )

        memory_kwargs = loop_memory_kwargs(
            conn,
            chat_id=request.chat_id,
            session_id=request.thread_id,
            operator_id=request.operator_id,
        )
        config = LoopConfig(
            caller_name=request.surface,
            surface=request.surface,
            tools=tools,
            intent=intent_label,
            messages=msgs,
            context_frame=context_frame,
            exposure_scopes=(
                *((context_frame.memory_exposure_scope(
                    operator_id=request.operator_id or context_operator_id(conn, chat_id=request.chat_id),
                    surface=request.surface, task_type=intent_label,
                    project_id=request.project_id,
                ),) if context_frame is not None else ()),
                *request.exposure_scopes,
            ),
            max_rounds=max_rounds,
            effective_project_id=request.project_id,
            conn=conn,
            chat_id=request.chat_id,
            enable_fabrication_guard=request.enable_fabrication_guard,
            round_context_provider=(
                CompositeRoundContext([
                    TelegramNotificationsProvider(drain_notifications),
                    DbDriftProvider(),
                ])
                if request.surface == "chat"
                else CompositeRoundContext([DbDriftProvider()])
            ),
            loop_start_utc=datetime.now(timezone.utc),
            blocked_tool_names=blocked_tool_names or frozenset(),
            show_thinking_window=show_thinking,
            on_stream_delta=request.on_stream_delta,
            progress_cb=request.progress_cb,
            required_tools_before_reply=request.required_tools_before_reply,
            digest_tool_results=request.harness.digest_tool_results if request.harness else False,
            explicit_operator_instruction=request.explicit_operator_instruction,
            operator_request_text=request.operator_request_text or request.user_text,
            execution_context={
                **(request.execution_context or {}),
                "operator_request_text": request.operator_request_text or request.user_text,
            },
            trace_context=context,
            **memory_kwargs,
        )
        try:
            loop_result = run_agent_loop(config)
            if request.apply_shared_post_processing:
                result = apply_shared_post_loop(
                    conn,
                    request.user_text,
                    loop_result,
                    effective_project_id=request.project_id,
                    context_frame=context_frame,
                    chat_id=request.chat_id,
                )
                persistence_status = "completed"
            else:
                result = loop_result.reply
                persistence_status = "not_requested"
            if not request.trace_finish_owned_by_caller:
                _emit_trace_best_effort(
                    emit_run_finish,
                    context,
                    status="completed",
                    exit_reason=loop_result.exit_reason,
                    final_output=result,
                    persistence_status=persistence_status,
                    anomaly_flags=["loop_exhaustion"] if loop_result.exit_reason in {"budget_exhausted", "exhausted"} else [],
                    metadata={
                        "round_count": loop_result.round_count,
                        "guard_results": loop_result.guard_results,
                        "project_id": request.project_id,
                        "operator_id": request.operator_id,
                        "chat_id": request.chat_id,
                    },
                )
            return result, loop_result, context_frame
        except Exception as exc:
            if not request.trace_finish_owned_by_caller:
                _emit_trace_best_effort(
                    emit_run_finish,
                    context,
                    status="error",
                    exit_reason=type(exc).__name__,
                    persistence_status="error",
                    anomaly_flags=["error"],
                    metadata={"error_class": type(exc).__name__},
                )
            raise


def run_telegram_loop(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    *,
    build_messages_fn,
    max_rounds: int | None = None,
    progress_cb: ProgressCallback | None = None,
    on_stream_delta: Callable[[str | None], None] | None = None,
    required_tools_before_reply: frozenset[str] | None = None,
    operator_id: str | None = None,
    trace_context: TraceContext | None = None,
    trace_finish_owned_by_caller: bool = False,
) -> Any:
    """Run Telegram context and loop under a caller-owned or local trace."""
    context = trace_context or TraceContext.create(
        surface="chat",
        task="telegram",
        thread_id=str(chat_id),
    )
    with trace_context_scope(context):
        _emit_trace_best_effort(
            emit_run_start,
            context,
            metadata={
                "surface": "chat",
                "task": "telegram",
                "session_id": str(chat_id),
                "chat_id": chat_id,
                "max_rounds_override": max_rounds,
                "entrypoint": "run_telegram_loop",
            },
            payload={"user_input": user_text},
        )
        try:
            result = _run_telegram_loop_body(
                conn,
                user_text,
                chat_id,
                build_messages_fn=build_messages_fn,
                max_rounds=max_rounds,
                progress_cb=progress_cb,
                on_stream_delta=on_stream_delta,
                required_tools_before_reply=required_tools_before_reply,
                operator_id=operator_id,
                trace_context=context,
                trace_finish_owned_by_caller=trace_finish_owned_by_caller,
            )
            if not trace_finish_owned_by_caller:
                _emit_trace_best_effort(
                    emit_run_finish,
                    context,
                    status="completed",
                    exit_reason=result[1].exit_reason,
                    final_output=result[0],
                    persistence_status="completed",
                    metadata={
                        "round_count": result[1].round_count,
                        "guard_results": result[1].guard_results,
                        "project_id": result[2],
                        "intent": result[3],
                        "chat_id": chat_id,
                    },
                )
            return result
        except Exception as exc:
            if not trace_finish_owned_by_caller:
                _emit_trace_best_effort(
                    emit_run_finish,
                    context,
                    status="error",
                    exit_reason=type(exc).__name__,
                    persistence_status="error",
                    anomaly_flags=["error"],
                    metadata={
                        "error_class": type(exc).__name__,
                        "chat_id": chat_id,
                    },
                )
            raise


def _run_telegram_loop_body(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    *,
    build_messages_fn,
    max_rounds: int | None,
    progress_cb: ProgressCallback | None,
    on_stream_delta: Callable[[str | None], None] | None,
    required_tools_before_reply: frozenset[str] | None,
    operator_id: str | None,
    trace_context: TraceContext,
    trace_finish_owned_by_caller: bool,
) -> Any:
    """Build Telegram context and execute the shared surface loop."""
    from src.agent.intent import label_intent
    from src.repositories.operator_state_repo import get_thinking_window_enabled
    from src.db.connection import scoped_read

    operator_id = operator_id if operator_id is not None else context_operator_id(conn, chat_id=chat_id)
    intent_label = label_intent(user_text)
    messages, effective_project_id, context_frame = build_messages_fn(
        conn, user_text, chat_id, intent=intent_label, operator_id=operator_id,
    )
    prepared = prepare_loop(
        conn,
        user_text,
        effective_project_id=effective_project_id,
        chat_id=chat_id,
        max_rounds_override=max_rounds,
        surface="chat",
        unified_v2=True,
        operator_id=operator_id,
    )
    role = resolve_role_for_v2(intent_label)
    request = LoopRequest(
        surface="chat",
        user_text=user_text,
        role=role,
        chat_id=chat_id,
        project_id=effective_project_id,
        harness=HarnessProfile(
            digest_tool_results=(role == AgentRole.HOST),
            context_profile=_role_context_profile(role),
        ),
        max_rounds=max_rounds,
        progress_cb=progress_cb,
        on_stream_delta=on_stream_delta,
        required_tools_before_reply=required_tools_before_reply,
        explicit_operator_instruction=_trusted_explicit_operator_instruction(user_text),
        exposure_scopes=prepared.exposure_scopes,
        operator_id=operator_id,
        thread_id=str(chat_id),
        trace_context=trace_context,
        preparation_version=prepared.preparation_version,
        preparation_snapshot=prepared.snapshot,
        trace_finish_owned_by_caller=trace_finish_owned_by_caller,
    )
    messages.extend(prepared.extra_messages)

    show_thinking = False
    if conn is not None:
        show_thinking = get_thinking_window_enabled(conn)
    else:
        with scoped_read() as rconn:
            show_thinking = get_thinking_window_enabled(rconn)

    result, loop_result, _ = run_surface_loop(
        conn,
        request,
        messages=messages,
        context_frame=context_frame,
        tools=prepared.tools,
        intent=intent_label,
        blocked_tool_names=prepared.blocked_tool_names,
        max_rounds=prepared.max_rounds,
        show_thinking=show_thinking,
    )
    return result, loop_result, effective_project_id, intent_label
