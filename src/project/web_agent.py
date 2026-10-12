"""Dashboard project agent with per-user context and execution policy."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import uuid4

from config import defaults
from src.agent.core_loop import run_agent_loop
from src.agent.loop_config import LoopConfig
from src.agent.prepare_loop import prepare_loop
from src.agent.web_chat_session import (
    normalize_web_session_history,
    session_context_notice,
)
from src.context.engine import ContextEngine
from src.db.connection import scoped_read, scoped_write
from src.observability.contracts import TraceContext, trace_context_scope
from src.observability.lifecycle import (
    context_composition_metadata,
    emit_event,
    emit_run_finish,
    emit_run_start,
    tool_contract_metadata,
)
from src.repositories.project_chat_repo import (
    get_project_chat_messages,
    save_project_chat_message,
)
from src.repositories.project_repo import get_project

logger = logging.getLogger(__name__)


def _read_only_tools(tools: list[dict]) -> list[dict]:
    from src.tools.registry import TOOL_BY_NAME

    return [
        tool for tool in tools
        if (contract := TOOL_BY_NAME.get(tool.get("function", {}).get("name", "")))
        and contract.side_effect_type in {"none", "read"}
    ]


def _save_message(
    conn: sqlite3.Connection | None,
    write_conn: sqlite3.Connection | None,
    *,
    use_scoped_writes: bool,
    project_id: str,
    owner_user_id: str,
    role: str,
    content: str,
    turn_id: str,
) -> None:
    if use_scoped_writes:
        with scoped_write() as active:
            save_project_chat_message(
                active, project_id, role, content, owner_user_id=owner_user_id,
                turn_id=turn_id,
            )
    elif write_conn is not None:
        save_project_chat_message(
            write_conn, project_id, role, content, owner_user_id=owner_user_id,
            turn_id=turn_id,
        )
        write_conn.commit()
    elif conn is not None:
        save_project_chat_message(
            conn, project_id, role, content, owner_user_id=owner_user_id,
            turn_id=turn_id,
        )
        conn.commit()
    else:
        with scoped_write() as active:
            save_project_chat_message(
                active, project_id, role, content, owner_user_id=owner_user_id,
                turn_id=turn_id,
            )


def _resolve_operator_id(
    conn: sqlite3.Connection | None,
    operator_id: str | None,
) -> str | None:
    """Resolve the authenticated operator before context composition begins."""
    if operator_id is not None:
        return operator_id
    from src.agent.memory_loop_helpers import context_operator_id

    if conn is not None:
        return context_operator_id(conn)
    with scoped_read() as read_conn:
        return context_operator_id(read_conn)


def project_web_agent(
    conn: sqlite3.Connection | None,
    project_id: str,
    user_message: str,
    *,
    write_conn: sqlite3.Connection | None = None,
    use_scoped_writes: bool = False,
    operator_id: str | None = None,
    dashboard_permissions: Iterable[str] = (),
    read_only_execution: bool = False,
    persist_messages: bool = True,
    session_messages: Iterable[Mapping[str, object]] = (),
    turn_id: str | None = None,
) -> str:
    """Run one project chat request under one truthful top-level trace."""
    active_turn_id = turn_id or uuid4().hex
    op_id = _resolve_operator_id(conn, operator_id)
    session_id = f"web:{op_id}:{project_id}"
    context = TraceContext.create(
        surface="web",
        task="project_web",
        thread_id=session_id,
        turn_id=active_turn_id,
    )

    with trace_context_scope(context):
        emit_run_start(
            context,
            metadata={
                "surface": "web",
                "task": "project_web",
                "entrypoint": "project_web_agent",
                "project_id": project_id,
                "operator_id": op_id,
                "session_id": session_id,
                "read_only_execution": read_only_execution,
                "persist_messages": persist_messages,
            },
            payload={"user_input": user_message},
        )
        try:
            reply, loop_result = _project_web_agent_body(
                conn,
                project_id,
                user_message,
                write_conn=write_conn,
                use_scoped_writes=use_scoped_writes,
                operator_id=op_id,
                dashboard_permissions=dashboard_permissions,
                read_only_execution=read_only_execution,
                persist_messages=persist_messages,
                session_messages=session_messages,
                active_turn_id=active_turn_id,
                session_id=session_id,
                trace_context=context,
            )
            emit_run_finish(
                context,
                status="completed",
                exit_reason=loop_result.exit_reason,
                final_output=reply,
                persistence_status="completed" if persist_messages else "not_requested",
                anomaly_flags=(
                    ["loop_exhaustion"]
                    if loop_result.exit_reason in {"budget_exhausted", "exhausted"}
                    else []
                ),
                metadata={
                    "round_count": loop_result.round_count,
                    "guard_results": loop_result.guard_results,
                    "project_id": project_id,
                    "operator_id": op_id,
                    "session_id": session_id,
                },
            )
            return reply
        except Exception as exc:
            emit_run_finish(
                context,
                status="error",
                exit_reason=type(exc).__name__,
                persistence_status="error",
                anomaly_flags=["error"],
                metadata={
                    "error_class": type(exc).__name__,
                    "project_id": project_id,
                    "operator_id": op_id,
                    "session_id": session_id,
                },
            )
            raise


def _project_web_agent_body(
    conn: sqlite3.Connection | None,
    project_id: str,
    user_message: str,
    *,
    write_conn: sqlite3.Connection | None,
    use_scoped_writes: bool,
    operator_id: str | None,
    dashboard_permissions: Iterable[str],
    read_only_execution: bool,
    persist_messages: bool,
    session_messages: Iterable[Mapping[str, object]],
    active_turn_id: str,
    session_id: str,
    trace_context: TraceContext,
) -> tuple[str, Any]:
    """Compose project context, execute the loop, and finish persistence."""
    permissions = tuple(sorted({str(item) for item in dashboard_permissions}))
    ephemeral_history = (
        normalize_web_session_history(session_messages)
        if read_only_execution and not persist_messages
        else []
    )

    if conn is not None:
        project = get_project(conn, project_id)
        if project is None:
            raise ValueError(f"Project {project_id} not found")
        history_rows = get_project_chat_messages(
            conn, project_id, limit=20, owner_user_id=operator_id,
        )
        engine = ContextEngine(conn)
        profile = "bootstrap" if getattr(defaults, "ORCHESTRATOR_MODE", False) else "full"
        history = [{"role": row["role"], "content": row["content"]} for row in history_rows]
        history.extend(ephemeral_history)
        frame = engine.build_for_web_chat(
            user_text=user_message,
            project_id=project_id,
            conversation_history=history,
            profile=profile,
            operator_id=operator_id,
            session_id=session_id,
        )
    else:
        with scoped_read() as read_conn:
            project = get_project(read_conn, project_id)
            if project is None:
                raise ValueError(f"Project {project_id} not found")
            history_rows = get_project_chat_messages(
                read_conn, project_id, limit=20, owner_user_id=operator_id,
            )
            engine = ContextEngine(read_conn)
            profile = "bootstrap" if getattr(defaults, "ORCHESTRATOR_MODE", False) else "full"
            history = [{"role": row["role"], "content": row["content"]} for row in history_rows]
            history.extend(ephemeral_history)
            frame = engine.build_for_web_chat(
                user_text=user_message,
                project_id=project_id,
                conversation_history=history,
                profile=profile,
                operator_id=operator_id,
                session_id=session_id,
            )

    if persist_messages:
        _save_message(
            conn, write_conn, use_scoped_writes=use_scoped_writes,
            project_id=project_id, owner_user_id=operator_id, role="user",
            content=user_message, turn_id=active_turn_id,
        )

    from src.agent.web_response_style import build_web_response_style

    messages = frame.to_messages()
    messages.append(build_web_response_style(resource_type="project"))
    if ephemeral_history:
        messages.append(session_context_notice())
    messages.append({"role": "user", "content": user_message})
    prepared = prepare_loop(
        conn, user_message, effective_project_id=project_id,
        surface="web", operator_id=operator_id,
    )
    messages.extend(prepared.extra_messages)

    from src.agent.memory_loop_helpers import loop_memory_kwargs

    if persist_messages:
        from src.agent.remember_parser import try_persist_remember_forbidden
        if use_scoped_writes:
            with scoped_write() as active:
                try_persist_remember_forbidden(active, user_message, operator_id=operator_id)
        else:
            try_persist_remember_forbidden(
                write_conn or conn, user_message, operator_id=operator_id,
            )

    tools = _read_only_tools(prepared.tools) if read_only_execution else prepared.tools
    tool_names = [
        str(tool.get("function", {}).get("name") or "")
        for tool in tools
        if tool.get("function", {}).get("name")
    ]
    emit_event(
        trace_context,
        "context.composed",
        metadata={
            **context_composition_metadata(
                messages,
                profile=profile,
                context_frame=frame,
            ),
            **tool_contract_metadata(tools),
            "project_id": project_id,
            "operator_id": operator_id,
            "session_id": session_id,
            "intent": prepared.intent,
            "tool_names": tool_names,
            "preparation_version": prepared.preparation_version,
        },
        payload={"ordered_messages": messages},
    )
    emit_event(
        trace_context,
        "prepare.snapshot",
        metadata={
            "preparation_version": prepared.preparation_version,
            **prepared.snapshot,
            "runtime_tool_names": tool_names,
            "read_only_execution": read_only_execution,
        },
    )

    memory_kwargs = loop_memory_kwargs(
        conn, session_id=session_id, operator_id=operator_id,
    )
    config = LoopConfig(
        caller_name="project_web",
        surface=prepared.surface,
        tools=tools,
        intent=prepared.intent,
        messages=messages,
        context_frame=frame,
        exposure_scopes=(
            frame.memory_exposure_scope(
                operator_id=operator_id, surface="web", task_type="chat", project_id=project_id,
            ),
            *getattr(prepared, "exposure_scopes", ()),
        ),
        max_rounds=prepared.max_rounds,
        effective_project_id=project_id,
        conn=conn,
        write_connection_factory=scoped_write if use_scoped_writes else None,
        blocked_tool_names=prepared.blocked_tool_names,
        execution_context={
            "read_only_execution": read_only_execution,
            "dashboard_permissions": permissions,
            "actor_user_id": operator_id,
        },
        trace_context=trace_context,
        **memory_kwargs,
    )
    loop_result = run_agent_loop(config)

    if persist_messages:
        from src.agent.post_loop import apply_shared_post_loop
        if use_scoped_writes:
            with scoped_write() as active:
                reply = apply_shared_post_loop(
                    active, user_message, loop_result,
                    effective_project_id=project_id, context_frame=frame,
                )
        else:
            reply = apply_shared_post_loop(
                write_conn or conn, user_message, loop_result,
                effective_project_id=project_id, context_frame=frame,
            )
        _save_message(
            conn, write_conn, use_scoped_writes=use_scoped_writes,
            project_id=project_id, owner_user_id=operator_id, role="assistant",
            content=reply, turn_id=active_turn_id,
        )

        if loop_result.round_count >= 2:
            from src.learning.outbox import capture_learning
            learning_kwargs = dict(
                surface="project_web",
                conversation_id=f"{operator_id}:{project_id}",
                messages=loop_result.messages,
                operator_id=operator_id or "default_operator",
                project_id=project_id,
                round_count=loop_result.round_count,
                tool_call_count=sum(len(round_) for round_ in loop_result.tool_history),
            )
            if use_scoped_writes:
                with scoped_write() as active:
                    capture_learning(active, **learning_kwargs)
            else:
                capture_learning(write_conn or conn, **learning_kwargs)
        if conn is not None:
            conn.commit()
        return reply, loop_result

    from src.agent.grounded_finalizer import finalize_grounded_reply

    finalized = finalize_grounded_reply(
        loop_result.reply,
        loop_result=loop_result,
        conn=conn,
        effective_project_id=project_id,
        surface="web",
    )
    return finalized.text, loop_result
