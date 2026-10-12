"""Unified entry for inbound email / reply agent processing."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from src.email.analysis_execution import FollowupIntent, inbound_analysis_scope

from src.agent.specialists.email_runner import (
    EmailAgentResult,
    run_email_specialist_loop,
    should_use_email_specialist,
)
from src.observability.contracts import (
    TraceContext,
    current_trace_context,
    trace_context_scope,
)
from src.observability.lifecycle import emit_run_finish, emit_run_start


def _emit_trace_best_effort(emitter: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Keep synchronous trace sink failures out of the email business path."""
    try:
        emitter(*args, **kwargs)
    except Exception:
        pass


def run_inbound_email_agent(
    conn: Any,
    user_text: str,
    chat_id: int,
    *,
    max_rounds: int = 5,
    trace_context: TraceContext | None = None,
) -> EmailAgentResult | Any:
    """Dispatch inbound email through one truthful top-level email trace."""
    active_context = current_trace_context()
    context = trace_context or active_context
    owns_context = context is None
    if context is None:
        context = TraceContext.create(
            surface="email",
            task="email",
            thread_id=str(chat_id),
        )
    bind_scope = (
        trace_context_scope(context)
        if active_context is not context
        else nullcontext(context)
    )

    with bind_scope:
        if owns_context:
            _emit_trace_best_effort(
                emit_run_start,
                context,
                metadata={
                    "surface": "email",
                    "task": "email",
                    "session_id": str(chat_id),
                    "chat_id": chat_id,
                    "max_rounds": max_rounds,
                    "entrypoint": "run_inbound_email_agent",
                },
                payload={"user_input": user_text},
            )
        try:
            if should_use_email_specialist():
                result = run_email_specialist_loop(
                    conn,
                    user_text,
                    chat_id,
                    max_rounds=max_rounds,
                    trace_context=context,
                )
            else:
                from src.telegram.agent_chat import agent_loop

                result = agent_loop(
                    conn,
                    user_text,
                    chat_id,
                    max_rounds=max_rounds,
                    trace_context=context,
                )
            if isinstance(result, EmailAgentResult):
                result.trace_id = context.trace_id
                result.run_id = context.run_id
                result.turn_id = context.turn_id
            if owns_context:
                _emit_trace_best_effort(
                    emit_run_finish,
                    context,
                    status="completed",
                    exit_reason="completed",
                    final_output=inbound_agent_reply(result),
                    persistence_status="completed",
                    metadata={
                        "surface": "email",
                        "task": "email",
                        "chat_id": chat_id,
                        "specialist_mode": should_use_email_specialist(),
                    },
                )
            return result
        except Exception as exc:
            if owns_context:
                _emit_trace_best_effort(
                    emit_run_finish,
                    context,
                    status="error",
                    exit_reason=type(exc).__name__,
                    persistence_status="error",
                    anomaly_flags=["error"],
                    metadata={
                        "surface": "email",
                        "task": "email",
                        "chat_id": chat_id,
                        "error_class": type(exc).__name__,
                    },
                )
            raise


def inbound_agent_reply(result: EmailAgentResult | Any) -> str:
    if hasattr(result, "reply"):
        return result.reply
    return str(result)


class ProjectUpdateAgentAnalysis(BaseModel):
    """Correlated agent output before review-gated persistence/presentation."""
    model_config = ConfigDict(frozen=True)

    reply: str | None
    stored: dict[str, Any] | None
    trace_id: Any = None
    run_id: Any = None
    turn_id: Any = None
    followup_intents: tuple[FollowupIntent, ...] = ()


def run_project_update_analysis(
    *, project_id: str, email_id: str, user_text: str, chat_id: int,
) -> ProjectUpdateAgentAnalysis:
    """Run the project-update agent without holding a caller write connection."""
    from src.tools.chat_handlers import clear_email_analysis_result, get_email_analysis_result

    clear_email_analysis_result(project_id=project_id, analysis_ref=email_id)
    # Analysis may only stage source-bound follow-ups; no business write commits.
    from src.tools.executor import trusted_execution_context

    with inbound_analysis_scope(project_id, email_id) as policy:
        with trusted_execution_context(inbound_analysis_policy=policy):
            agent_result = run_inbound_email_agent(None, user_text, chat_id, max_rounds=5)
            reply = inbound_agent_reply(agent_result)
            stored = get_email_analysis_result(project_id=project_id, analysis_ref=email_id)
    return ProjectUpdateAgentAnalysis(
        reply=reply,
        stored=stored,
        trace_id=getattr(agent_result, "trace_id", None),
        run_id=getattr(agent_result, "run_id", None),
        turn_id=getattr(agent_result, "turn_id", None),
        followup_intents=tuple(policy.followups),
    )
