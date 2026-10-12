"""
Unified agent loop for Telegram free text.

Single entry point: ``agent_loop()`` (aliased as ``agent_respond`` for
backward-compatible callers).

The model IS the agent.  The code is the harness.
One loop, one exit condition, one tool dispatch dict.
The model decides what to do and when to stop.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Callable

from config import defaults
from src.db.connection import scoped_read, scoped_write

_ORIGINAL_SCOPED_READ = scoped_read
_ORIGINAL_SCOPED_WRITE = scoped_write


def _scoped_read_connection():
    """Resolve patched scoped read providers without bypassing local test patches."""
    if scoped_read is not _ORIGINAL_SCOPED_READ:
        return scoped_read()
    from src.db.connection import scoped_read as current_scoped_read
    return current_scoped_read()


def _scoped_write_connection():
    """Resolve patched scoped write providers without bypassing local test patches."""
    if scoped_write is not _ORIGINAL_SCOPED_WRITE:
        return scoped_write()
    from src.db.connection import scoped_write as current_scoped_write
    return current_scoped_write()
from src.models.context_frame import ContextFrame
from src.llm.client import llm_call, llm_call_stream
from src.observability.contracts import TraceContext
from src.observability.progress import ProgressCallback, emit_progress
from src.telegram.notification_queue import drain_notifications
from src.telegram.deterministic_commands import DeterministicCommand
from src.context.engine import ContextEngine
from src.context.skills_index import build_skills_block
from src.agent.prepare_loop import prepare_loop
from src.agent.round_budget import MAX_AGENT_ROUNDS
from src.utils.operator_language import (
    operator_language_instruction as _operator_language_instruction,
    read_response_language as _read_response_language,
    summary_action_language_rule as _summary_action_language_rule,
)

# Re-exports from src.agent.safety — other modules import these from here
from src.agent.safety import (  # noqa: F401
    _REASONING_PREFIXES,
    extract_fallback_text as _extract_fallback_text,
    _FABRICATION_RE,
    extract_reasoning as _extract_reasoning,
    format_reasoning_blockquote as _format_reasoning_blockquote,
    sanitize_for_user,
    normalize_assistant_message,
    _DRAFT_SIGNALS,
    looks_like_fabricated_draft as _looks_like_fabricated_draft,
    summarise_on_exhaustion as _summarise_on_exhaustion,
)

# Re-exports from src.agent.intent — other modules import these from here
from src.agent.intent import (  # noqa: F401
    _CASUAL_PATTERNS,
    is_casual_message as _is_casual_message,
    _PORTFOLIO_PATTERNS,
    is_portfolio_query as _is_portfolio_query,
    detect_project_id as _detect_project_id,
    is_complex_request as _is_complex_request,
    _QUERY_KEYWORDS,
    _ACTION_KEYWORDS,
    classify_intent as _classify_intent,
)

# Re-exports from src.telegram.session_persistence — tests import these from here
from src.telegram.session_persistence import (  # noqa: F401
    _load_progress_prompt,
    _SUMMARY_MAX_TURNS,
    _SUMMARY_EXPIRY_HOURS,
    _SESSION_GAP_HOURS,
    _update_conversation_summary,
    _get_conversation_summary,
    _parse_summary_ts,
    _PROGRESS_SCHEMA,
    _FALLBACK_LOG_MAX,
    _PROGRESS_EXPIRY_DAYS,
    _update_progress,
    _get_progress,
    _format_progress,
    _SESSION_BOUNDARY_HOURS,
    _maybe_save_session_summary,
    _persist_summary,
)

# Re-exports from src.telegram.memory_flush — tests import these from here
from src.telegram.memory_flush import (  # noqa: F401
    _flush_memories,
    _build_existing_state_summary,
    _do_flush,
    _notify_learning,
)

# Backward-compat alias: old callers use sanitize_for_user via this module
sanitize_for_user = sanitize_for_user  # re-exported above

logger = logging.getLogger(__name__)


def _in_inbound_analysis() -> bool:
    """Telegram fallback must leave business and conversation writes to the A receipt."""
    from src.email.analysis_execution import active_analysis_policy

    return active_analysis_policy() is not None

# Backward-compatible alias for timeout math in handlers_freetext.
MAX_ROUNDS = MAX_AGENT_ROUNDS

# Intents that skip post-loop background LLM + heavy DB work (flush, progress, session summary).
_SKIP_BG_INTENTS = frozenset({"query", "casual"})


@dataclass(frozen=True)
class AgentChatResult:
    reply: str
    effective_project_id: str | None = None


# ── Auto-context injection (P2) ────────────────────────────────────────

def _infer_project_from_conversation(conn: sqlite3.Connection, chat_id: int) -> str | None:
    row = conn.execute(
        """SELECT project_id FROM telegram_messages
           WHERE chat_id = ? AND project_id IS NOT NULL
             AND message_kind IN ('text', 'command', 'command_reply')
           ORDER BY created_at DESC LIMIT 1""",
        (chat_id,),
    ).fetchone()
    return row["project_id"] if row else None


# ── Contextual suggestions (P12) ──────────────────────────────────────

def _maybe_suggest(conn: sqlite3.Connection, project_id: str | None, response_text: str) -> str:
    from src.advisory.suggest import maybe_suggest_advisory
    return maybe_suggest_advisory(conn, project_id, response_text)


def _apply_final_telegram_advisory(
    conn: sqlite3.Connection | None,
    *,
    user_text: str,
    effective_project_id: str | None,
    response_text: str,
    loop_result: Any,
) -> str:
    """Apply the last Telegram-only transform, then revalidate served text."""
    if not _detect_project_id(user_text):
        return response_text

    if conn is not None:
        candidate = _maybe_suggest(conn, effective_project_id, response_text)
    else:
        with scoped_read() as rconn:
            candidate = _maybe_suggest(rconn, effective_project_id, response_text)

    from src.agent.grounded_finalizer import finalize_grounded_reply

    return finalize_grounded_reply(
        candidate,
        loop_result=loop_result,
        conn=conn,
        effective_project_id=effective_project_id,
        surface="chat",
    ).text


# ── L3 — self-correction logging ───────────────────────────────────────

_CORRECTABLE_FIELDS = frozenset({"current_milestone", "ball_with", "project_status"})


def _extract_context_value(context_project_truth: str, field: str) -> str | None:
    """Extract a field's value from the project_truth text block."""
    patterns = {
        "current_milestone": r"Milestone:\s*(\S+)",
        "ball_with": r"Ball:\s*(\S+)",
        "project_status": r"Status:\s*(\S+)",
    }
    pattern = patterns.get(field)
    if not pattern:
        return None
    m = re.search(pattern, context_project_truth or "")
    return m.group(1).rstrip("|").strip() if m else None


def _maybe_log_correction(
    conn: sqlite3.Connection | None,
    project_id: str,
    field: str,
    new_value: str,
    context_project_truth: str,
) -> None:
    """Log to project_corrections if the agent is correcting a stale session value."""
    if field not in _CORRECTABLE_FIELDS:
        return
    prior = _extract_context_value(context_project_truth, field)
    if not prior or prior == new_value:
        return
    try:
        from src.repositories.project_corrections_repo import insert_correction
        if conn is not None:
            insert_correction(
                conn,
                project_id=project_id,
                field=field,
                wrong_value=prior,
                correct_value=new_value,
            )
            conn.commit()
        else:
            with scoped_write() as wconn:
                insert_correction(
                    wconn,
                    project_id=project_id,
                    field=field,
                    wrong_value=prior,
                    correct_value=new_value,
                )
        logger.info(
            "Correction logged for %s.%s: %r → %r", project_id, field, prior, new_value
        )
    except Exception:
        logger.exception("Failed to log correction for %s.%s", project_id, field)


# ── Prior-session memory injection (cross-session continuity) ─────────

_PRIOR_SESSION_MAX_AGE_HOURS = 24


def _get_prior_session_summary(conn: sqlite3.Connection, chat_id: int) -> str | None:
    """Return a <prior-session> block if a session summary exists within 24h.

    Skips injection when no prior session exists or the last one is stale,
    keeping the system prompt clean for brand-new interactions.
    """
    try:
        from src.repositories.session_summary_repo import get_recent_summaries
        summaries = get_recent_summaries(conn, chat_id, limit=1)
        if not summaries:
            return None
        last = summaries[0]
        ended_at_str = last.get("ended_at") or ""
        if not ended_at_str:
            return None
        ended_at = datetime.fromisoformat(ended_at_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - ended_at).total_seconds() / 3600
        if age_hours > _PRIOR_SESSION_MAX_AGE_HOURS:
            return None
        parts = [f'<prior-session age_hours="{age_hours:.0f}">']
        parts.append(last["summary_text"])
        if last.get("projects_discussed"):
            parts.append(f"Projects discussed: {last['projects_discussed']}")
        if last.get("decisions_made"):
            parts.append(f"Decisions made: {last['decisions_made']}")
        if last.get("open_questions"):
            parts.append(f"Open questions: {last['open_questions']}")
        parts.append("</prior-session>")
        return "\n".join(parts)
    except Exception:
        logger.debug("Failed to load prior session summary", exc_info=True)
        return None


# ── Active project persistence (short scoped_write burst) ──────────────

_loop_operator_id: ContextVar[str | None] = ContextVar("loop_operator_id", default=None)


def _maybe_set_active_project(
    chat_id: int,
    project_id: str,
    conn: sqlite3.Connection | None = None,
    operator_id: str | None = None,
) -> None:
    """Persist active project binding via scoped_write (production path)."""
    from src.agent.active_project import set_active_project_id

    resolved = operator_id if operator_id is not None else _loop_operator_id.get()
    if conn is not None:
        set_active_project_id(conn, chat_id, project_id, operator_id=resolved)
        return
    with _scoped_write_connection() as wconn:
        set_active_project_id(wconn, chat_id, project_id, operator_id=resolved)


# ── Build initial messages ─────────────────────────────────────────────

def _build_initial_messages(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    *,
    intent: str | None = None,
    operator_id: str | None = None,
) -> tuple[list[dict], str | None, ContextFrame | None]:
    """Build system prompt + compressed history + context + user message.

    Returns (messages, effective_project_id, context_frame).
    """
    from src.agent.active_project import get_active_project_id
    from src.agent.guards import (
        is_ambiguous_project_reference,
        is_production_sign_off,
        needs_write_project_clarification,
    )
    explicit_pid_to_set: str | None = None
    resolved_intent = intent if intent is not None else _classify_intent(user_text)

    def _build_with_conn(rconn: sqlite3.Connection) -> tuple[list[dict], str | None, ContextFrame | None]:
        nonlocal explicit_pid_to_set

        casual = _is_casual_message(user_text)
        portfolio = _is_portfolio_query(user_text)
        ambiguous = is_ambiguous_project_reference(rconn, user_text)
        write_clarify = needs_write_project_clarification(
            user_text, conn=rconn, chat_id=chat_id,
        )
        # Harness policy: message-named project always beats sticky active
        from src.agent.project_scope import (
            project_scope_system_message,
            resolve_project_scope,
        )

        resolved_operator = operator_id if operator_id is not None else _loop_operator_id.get()
        scope = resolve_project_scope(
            rconn, user_text, chat_id, operator_id=resolved_operator
        )
        explicit_pid = scope.effective_project_id if scope.explicit_from_message else None
        if is_production_sign_off(user_text) and not explicit_pid:
            active_pid = get_active_project_id(
                rconn, chat_id, operator_id=resolved_operator
            )
            if active_pid:
                explicit_pid = active_pid

        if portfolio or write_clarify or ambiguous:
            effective_project_id = explicit_pid  # may still be set if named
            if portfolio:
                effective_project_id = None
        elif scope.explicit_from_message:
            effective_project_id = scope.effective_project_id
        elif scope.source in {"project_scope_none", "general_knowledge", "adhoc_compose"}:
            effective_project_id = None
        elif casual:
            effective_project_id = None
        else:
            effective_project_id = (
                scope.effective_project_id
                or _infer_project_from_conversation(rconn, chat_id)
            )

        if explicit_pid and not ambiguous:
            explicit_pid_to_set = explicit_pid

        _is_email_auto = (
            user_text.startswith("[INBOUND EMAIL]")
            or user_text.startswith("[INBOUND REPLY]")
        )
        orchestrator_host = (
            getattr(defaults, "ORCHESTRATOR_MODE", False)
            and resolved_intent not in ("email", "casual")
            and not _is_email_auto
        )
        context_profile = (
            "bootstrap"
            if orchestrator_host
            else "minimal"
            if resolved_intent == "query" and not effective_project_id
            else "full"
        )

        from src.agent.memory_loop_helpers import context_operator_id

        op_id = context_operator_id(rconn, chat_id=chat_id)
        engine = ContextEngine(rconn)
        frame = engine.build_for_chat(
            user_text=user_text,
            chat_id=chat_id,
            project_id=effective_project_id,
            profile=context_profile,
            operator_id=op_id,
        )

        messages = frame.to_messages()

        if context_profile != "bootstrap":
            skills_block = build_skills_block()
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = messages[0]["content"] + "\n\n" + skills_block

        lang = _read_response_language(rconn)
        # Embed policy in the primary system message AND as a trailing system
        # reminder so it overrides any hardcoded language lines in prompt templates.
        lang_policy = _operator_language_instruction(lang)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = (
                messages[0]["content"] + "\n\n## " + lang_policy
            )
        messages.append({
            "role": "system",
            "content": lang_policy,
        })

        # Cross-session continuity: inject most recent session summary if < 24h old.
        # Skip for email triage and bootstrap orchestrator host (deduped in profile).
        if not _is_email_auto and context_profile not in ("minimal", "bootstrap"):
            prior_session = _get_prior_session_summary(rconn, chat_id)
            if prior_session:
                messages.append({"role": "system", "content": prior_session})

        if _is_email_auto:
            from pathlib import Path
            _email_prompt_path = (
                Path(__file__).resolve().parents[2] / "prompts" / "email_analysis_instructions.md"
            )
            if _email_prompt_path.exists():
                email_instr = _email_prompt_path.read_text(encoding="utf-8")
                email_instr += "\n\n## " + lang_policy
                email_instr += "\n\n## Summary language\n" + _summary_action_language_rule(lang)
                messages.append({
                    "role": "system",
                    "content": email_instr,
                })

        messages.append({"role": "user", "content": user_text})

        scope_msg = project_scope_system_message(scope, user_text)
        if scope_msg and not portfolio:
            # Re-resolve scope label for sticky vs explicit after sign-off path
            if explicit_pid and not scope.explicit_from_message:
                from src.agent.project_scope import ProjectScope

                scope_msg = project_scope_system_message(
                    ProjectScope(explicit_pid, True, "explicit"),
                    user_text,
                )
            if scope_msg:
                messages.append(scope_msg)

        if not _is_email_auto and _is_complex_request(user_text):
            messages.append({
                "role": "user",
                "content": (
                    "<planning-hint>This is a multi-step request. "
                    "You MUST call `think` first to plan your approach before any other tool calls.</planning-hint>"
                ),
            })

        # Stash force flag on frame metadata for LoopConfig (if available)
        if frame is not None and hasattr(frame, "__dict__"):
            try:
                frame.__dict__["_project_scope_forced"] = bool(
                    explicit_pid or scope.explicit_from_message
                )
                frame.__dict__["_project_scope_typo_corrected"] = (
                    scope.source == "typo_corrected"
                )
            except Exception:
                pass

        return messages, effective_project_id, frame

    if conn is not None:
        result = _build_with_conn(conn)
    else:
        with scoped_read() as rconn:
            result = _build_with_conn(rconn)

    if explicit_pid_to_set and not _in_inbound_analysis():
        _maybe_set_active_project(chat_id, explicit_pid_to_set, conn)

    return result


# ── Simulated streaming for non-streamed responses ────────────────────

def _simulate_stream(
    text: str,
    on_delta: "Callable[[str], None]",
    chunk_size: int = 80,
) -> None:
    """Progressive reveal of already-complete text via delta callback."""
    for i in range(0, len(text), chunk_size):
        on_delta(text[i:i + chunk_size])
        time.sleep(0.03)


# ── Casual greeting fast path ────────────────────────────────────────────


def _casual_greeting_reply(
    user_text: str,
    chat_id: int,
    *,
    conn: sqlite3.Connection | None = None,
    progress_cb: ProgressCallback | None = None,
    on_stream_delta: "Callable[[str | None], None] | None" = None,
) -> AgentChatResult:
    """Minimal LLM call for greetings — shared by legacy and unified v2."""
    logger.debug("Intent=casual: no tools, minimal context, single LLM call")
    emit_progress(progress_cb, "Build context", "done", "Casual — skipped")
    step = "Agent round 1"
    emit_progress(progress_cb, step, "running", "Calling LLM")
    lang = _read_response_language(conn)
    lang_policy = _operator_language_instruction(lang)
    fallback = (
        "Hi! How can I help?"
        if lang == "en"
        else '你好！有咩可以幫到你？'
        if lang == "zh-TW"
        else '你好！有什么可以帮到你？'
    )
    minimal_messages = [
        {
            "role": "system",
            "content": (
                "You are AgentFIXBPro, a FIX protocol onboarding PM copilot. "
                "Keep the response brief and friendly.\n\n" + lang_policy
            ),
        },
        {"role": "user", "content": user_text},
    ]
    from src.observability.contracts import current_trace_context
    from src.observability.lifecycle import (
        context_composition_metadata, emit_event, tool_contract_metadata,
    )

    trace_context = current_trace_context()
    if trace_context is not None:
        emit_event(
            trace_context,
            "context.composed",
            metadata={
                **context_composition_metadata(
                    minimal_messages,
                    profile="telegram-casual",
                    context_frame=None,
                ),
                **tool_contract_metadata([]),
                "surface": "chat",
                "task": "telegram",
                "chat_id": chat_id,
                "intent": "casual",
                "tool_names": [],
            },
            payload={"ordered_messages": minimal_messages},
        )
    try:
        if on_stream_delta is not None:
            content = llm_call_stream(
                "status", minimal_messages, temperature=0.3, on_delta=on_stream_delta,
            )
        else:
            content = llm_call("status", minimal_messages, temperature=0.3)
    except Exception:
        emit_progress(progress_cb, step, "error", "LLM call failed")
        logger.exception("LLM call failed for casual message")
        return AgentChatResult(reply=fallback)
    emit_progress(progress_cb, step, "done", "Final answer ready")
    result = content if content else fallback
    if not _in_inbound_analysis():
        _persist_summary(conn, chat_id, user_text, result, skip_llm=True)
    return AgentChatResult(reply=result)


# ── The unified agent loop ─────────────────────────────────────────────



def _present_deterministic_command(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    command: DeterministicCommand,
    *,
    progress_cb: ProgressCallback | None,
    trace_context: TraceContext,
    trace_outcome: dict[str, Any],
) -> AgentChatResult:
    """Complete a routed shortcut through the established Telegram patch points."""
    emit_progress(progress_cb, "Build context", "done", command.context_progress)
    emit_progress(progress_cb, "Agent round 1", "done", command.round_progress)
    _persist_summary(conn, chat_id, user_text, command.reply, skip_llm=True)
    if command.project_id:
        _maybe_set_active_project(chat_id, command.project_id, conn)
    _emit_deterministic_context_trace(
        trace_context, user_text, shortcut=command.name,
        project_id=command.project_id,
    )
    trace_outcome.update({
        "exit_reason": command.exit_reason,
        "persistence_status": "completed",
        "intent": command.name,
    })
    return AgentChatResult(reply=command.reply, effective_project_id=command.project_id)


def _emit_deterministic_context_trace(
    trace_context: Any,
    user_text: str,
    *,
    shortcut: str,
    project_id: str | None,
) -> None:
    """Record the exact input consumed by a deterministic Telegram shortcut."""
    from src.observability.lifecycle import (
        context_composition_metadata, emit_event, tool_contract_metadata,
    )

    messages = [{"role": "user", "content": user_text}]
    emit_event(
        trace_context,
        "context.composed",
        metadata={
            **context_composition_metadata(
                messages,
                profile=f"telegram-deterministic:{shortcut}",
                context_frame=None,
            ),
            **tool_contract_metadata([]),
            "surface": "chat",
            "task": "telegram",
            "intent": shortcut,
            "project_id": project_id,
            "tool_names": [],
        },
        payload={"ordered_messages": messages},
    )
    emit_event(
        trace_context,
        "routing.shortcut",
        metadata={
            "shortcut": shortcut,
            "project_id": project_id,
            "observable_outcome": "deterministic_response",
        },
    )

def _telegram_tool_centric_scope(scope) -> tuple[str, list[str], str | None] | None:
    """Map canonical Telegram project resolution to a trusted typed-read scope."""
    if scope.source in {"adhoc_compose", "unresolved_explicit", "general_knowledge", "project_scope_none"}:
        return None
    if scope.effective_project_id:
        project_id = str(scope.effective_project_id)
        return "projects", [project_id], project_id
    return "portfolio", [], None




def _safe_telegram_run_start(context, **kwargs: Any) -> None:
    try:
        from src.observability.lifecycle import emit_run_start

        emit_run_start(context, **kwargs)
    except Exception:
        pass


def _safe_telegram_run_finish(context, **kwargs: Any) -> None:
    try:
        from src.observability.lifecycle import emit_run_finish

        emit_run_finish(context, **kwargs)
    except Exception:
        pass


def agent_loop(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    *,
    max_rounds: int | None = None,
    progress_cb: ProgressCallback | None = None,
    on_stream_delta: "Callable[[str | None], None] | None" = None,
    required_tools_before_reply: frozenset[str] | None = None,
    trace_context: "TraceContext | None" = None,
    operator_id: str | None = None,
    require_persisted_actor: bool = False,
) -> AgentChatResult:
    """Run one Telegram turn with lifecycle ownership at the public boundary."""
    from contextlib import nullcontext

    from src.observability.contracts import (
        TraceContext,
        current_trace_context,
        trace_context_scope,
    )
    active_context = current_trace_context()
    context = trace_context or active_context
    owns_context = context is None
    if context is None:
        context = TraceContext.create(
            surface="chat",
            task="telegram",
            thread_id=str(chat_id),
        )
    bind_scope = (
        trace_context_scope(context)
        if active_context is not context
        else nullcontext(context)
    )
    trace_outcome: dict[str, Any] = {
        "exit_reason": "completed",
        "persistence_status": "completed",
        "intent": None,
        "round_count": 0,
        "tool_call_count": 0,
    }

    with bind_scope:
        if owns_context:
            _safe_telegram_run_start(
                context,
                metadata={
                    "surface": "chat",
                    "task": "telegram",
                    "session_id": str(chat_id),
                    "chat_id": chat_id,
                    "max_rounds_override": max_rounds,
                    "entrypoint": "agent_loop",
                },
                payload={"user_input": user_text},
            )
        try:
            result = _agent_loop_body(
                conn,
                user_text,
                chat_id,
                max_rounds=max_rounds,
                progress_cb=progress_cb,
                on_stream_delta=on_stream_delta,
                required_tools_before_reply=required_tools_before_reply,
                trace_context=context,
                trace_outcome=trace_outcome,
                operator_id=operator_id,
                require_persisted_actor=require_persisted_actor,
            )
            if owns_context:
                exit_reason = str(trace_outcome.get("exit_reason") or "completed")
                _safe_telegram_run_finish(
                    context,
                    status=(
                        "unknown"
                        if trace_outcome.get("persistence_status") == "unknown"
                        else "completed"
                    ),
                    exit_reason=exit_reason,
                    final_output=result.reply,
                    persistence_status=str(
                        trace_outcome.get("persistence_status") or "completed"
                    ),
                    anomaly_flags=(
                        ["loop_exhaustion"]
                        if exit_reason in {"budget_exhausted", "exhausted"}
                        else []
                    ),
                    metadata={
                        "round_count": int(trace_outcome.get("round_count") or 0),
                        "project_id": result.effective_project_id,
                        "intent": trace_outcome.get("intent"),
                        "chat_id": chat_id,
                        "tool_call_count": int(
                            trace_outcome.get("tool_call_count") or 0
                        ),
                        "guard_results": list(
                            trace_outcome.get("guard_results") or []
                        ),
                    },
                )
            return result
        except Exception as exc:
            if owns_context:
                _safe_telegram_run_finish(
                    context,
                    status="error",
                    exit_reason=type(exc).__name__,
                    persistence_status="error",
                    anomaly_flags=["error"],
                    metadata={
                        "error_class": type(exc).__name__,
                        "chat_id": chat_id,
                        "intent": trace_outcome.get("intent"),
                    },
                )
            raise


def _agent_loop_body(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    *,
    max_rounds: int | None,
    progress_cb: ProgressCallback | None,
    on_stream_delta: "Callable[[str | None], None] | None",
    required_tools_before_reply: frozenset[str] | None,
    trace_context: "TraceContext",
    trace_outcome: dict[str, Any],
    operator_id: str | None = None,
    require_persisted_actor: bool = False,
) -> AgentChatResult:
    """Execute Telegram behavior while the public entry owns trace completion."""
    from src.observability.context_budget import reset_context_budget

    reset_context_budget()
    _loop_operator_id.set(operator_id)

    from config import defaults as _defaults

    analysis_only = _in_inbound_analysis()

    # A reply to an owned staged update is a continuation of the write flow,
    # not a new portfolio read. Resolve it before intent classification and
    # tool-centric serving so values such as "Waiting" cannot become filters.
    pending_reply = None
    if not analysis_only:
        from src.agent.pending_operation_router import try_pending_project_update_reply

        pending_reply = try_pending_project_update_reply(
            conn,
            user_text,
            chat_id=chat_id,
            operator_id=operator_id,
            require_persisted_actor=require_persisted_actor,
        )
    if pending_reply is not None:
        emit_progress(
            progress_cb,
            "Resolve pending update",
            "done",
            (
                "Staged update committed and read back"
                if pending_reply.committed
                else f"Pending update {pending_reply.action} resolved"
            ),
        )
        if on_stream_delta and pending_reply.reply:
            on_stream_delta(None)
            _simulate_stream(pending_reply.reply, on_stream_delta)
        _persist_summary(
            conn,
            chat_id,
            user_text,
            pending_reply.reply,
            skip_llm=True,
        )
        _maybe_set_active_project(
            chat_id,
            pending_reply.project_id,
            conn,
        )
        _emit_deterministic_context_trace(
            trace_context,
            user_text,
            shortcut=f"pending_project_update_{pending_reply.action}",
            project_id=pending_reply.project_id,
        )
        trace_outcome.update({
            "exit_reason": f"deterministic_pending_update_{pending_reply.action}",
            "persistence_status": (
                "committed"
                if pending_reply.committed
                else pending_reply.action
            ),
            "intent": "project_update_confirmation",
            "round_count": 0,
            "tool_call_count": 0 if pending_reply.action == "clarify" else 1,
            "guard_results": [],
        })
        return AgentChatResult(
            reply=pending_reply.reply,
            effective_project_id=pending_reply.project_id,
        )

    intent = _classify_intent(user_text)
    trace_outcome["intent"] = intent
    if intent == "casual" and not analysis_only:
        result = _casual_greeting_reply(
            user_text,
            chat_id,
            conn=conn,
            progress_cb=progress_cb,
            on_stream_delta=on_stream_delta,
        )
        trace_outcome.update({
            "exit_reason": "casual_reply",
            "persistence_status": "completed",
            "round_count": 1,
            "tool_call_count": 0,
        })
        return result

    # Closing must be resolved before reconciliation and either model path.
    # The executor returns read-only Dashboard guidance, never a terminal write.
    early_command = None
    if not analysis_only:
        from src.telegram.deterministic_commands import try_early_sign_off

        early_command = try_early_sign_off(conn, user_text, chat_id)
    if early_command is not None:
        return _present_deterministic_command(
            conn, user_text, chat_id, early_command,
            progress_cb=progress_cb, trace_context=trace_context,
            trace_outcome=trace_outcome,
        )

    # Email-to-project reconciliation is deterministic and durable. An explicit
    # operator update must not depend on a multi-round LLM stage/confirm chain.
    reconciliation_request = None
    if not analysis_only:
        from src.project_reconciliation.surface import classify_reconciliation_request

        reconciliation_request = classify_reconciliation_request(user_text)
    if reconciliation_request is not None:
        from src.project_reconciliation.surface import (
            classify_reconciliation_request,
            execute_reconciliation_request,
        )
        from src.agent.project_scope import resolve_project_scope

        emit_progress(
            progress_cb,
            "Reconcile project",
            "running",
            "Freezing and evaluating historical email evidence",
        )
        post_commit_effects: list[Callable[[], None]] = []
        with _scoped_write_connection() as reconciliation_conn:
            scope = resolve_project_scope(reconciliation_conn, user_text, chat_id)
            fallback_ids = (
                [scope.effective_project_id]
                if scope.effective_project_id
                else []
            )
            reconciliation_request = classify_reconciliation_request(
                user_text,
                fallback_project_ids=fallback_ids,
            )
            execution = execute_reconciliation_request(
                reconciliation_conn,
                request=reconciliation_request,
                operator_id=operator_id,
                chat_id=str(chat_id),
                trusted_surface="telegram",
                request_key=(
                    f"project-reconcile:telegram:{trace_context.turn_id}"
                    if trace_context.turn_id
                    else None
                ),
                post_commit=post_commit_effects,
            )
            selected_project_id = execution.request.project_id
            if selected_project_id and execution.result.current_project is not None:
                _maybe_set_active_project(
                    chat_id,
                    selected_project_id,
                    reconciliation_conn,
                )

        for effect in post_commit_effects:
            try:
                effect()
            except Exception:
                logger.exception("Committed reconciliation notification failed")
        receipt_unknown = False
        if execution.result.status == "pending_commit":
            from src.project_reconciliation.repository import get_operation
            from src.project_reconciliation.service_support import stored_result
            from src.project_reconciliation.surface import format_reconciliation_result

            try:
                with _scoped_read_connection() as receipt_conn:
                    operation = get_operation(receipt_conn, execution.result.operation_id)
                    receipt = stored_result(operation) if operation else None
                if (
                    receipt is None
                    or not receipt.committed
                    or receipt.status != "committed"
                    or receipt.operation_id != execution.result.operation_id
                    or receipt.project_id != execution.result.project_id
                ):
                    raise ValueError("Committed reconciliation receipt is missing or invalid")
                execution = replace(
                    execution, result=receipt, text=format_reconciliation_result(receipt),
                )
            except Exception:
                # The outer write already committed. Do not imply a rollback or
                # replay the command when the independent receipt read fails.
                logger.exception("Could not verify reconciliation receipt after outer commit")
                receipt_unknown = True
        reply = (
            f"**UNKNOWN：{selected_project_id or execution.result.project_id} "
            "的郵件核對結果未能確認。**"
            "外層 DB 交易已成功提交，但回讀／驗證收據失敗；"
            "不能當作回滾或已確認的更新。\n"
            f"• Operation：`{execution.result.operation_id}`\n"
            "請按此 Operation 及專案在 Dashboard／DB 手動核對實際狀態；"
            "確認前不要重試或重播此更新，以免重複寫入。"
            if receipt_unknown else execution.text
        )
        try:
            with _scoped_write_connection() as summary_conn:
                _persist_summary(summary_conn, chat_id, user_text, reply, skip_llm=True)
        except Exception:
            # The separately committed project command is not undone by a
            # conversation-summary failure; its receipt remains authoritative.
            logger.exception("Could not persist reconciliation conversation summary")

        emit_progress(
            progress_cb,
            "Reconcile project",
            "error" if receipt_unknown else "done" if execution.result.ok else "error",
            (
                "Receipt UNKNOWN; manual verification required"
                if receipt_unknown else
                "DB receipt confirmed" if execution.result.committed else
                "Reconciliation receipt ready"
            ),
        )
        if on_stream_delta and reply:
            try:
                on_stream_delta(None)
                _simulate_stream(reply, on_stream_delta)
            except Exception:
                # Streaming is best-effort after commit and receipt verification.
                # Preserve the authoritative reply and outcome for the caller.
                logger.exception("Committed reconciliation stream delivery failed")
        try:
            _emit_deterministic_context_trace(
                trace_context,
                user_text,
                shortcut="project_reconciliation",
                project_id=selected_project_id,
            )
        except Exception:
            # Observability must not change an already committed operation outcome.
            logger.exception("Committed reconciliation context trace failed")
        trace_outcome.update({
            "exit_reason": "deterministic_project_reconciliation",
            "persistence_status": (
                "unknown" if receipt_unknown else
                "committed" if execution.result.committed else execution.result.status
            ),
            "intent": "project_reconciliation",
            "round_count": 0,
            "tool_call_count": 1,
            "guard_results": [],
        })
        return AgentChatResult(
            reply=reply,
            effective_project_id=selected_project_id,
        )

    from src.assistant.tool_centric_serving import (
        should_attempt_tool_centric_serving,
        tool_centric_planner_model,
        try_tool_centric_read,
    )

    if not analysis_only and should_attempt_tool_centric_serving("telegram"):
        serving_conn = conn
        read_scope = None
        try:
            if serving_conn is None:
                read_scope = _scoped_read_connection()
                serving_conn = read_scope.__enter__()

            from src.agent.project_scope import resolve_project_scope

            resolved_scope = resolve_project_scope(serving_conn, user_text, chat_id)
            serving_scope = _telegram_tool_centric_scope(resolved_scope)
            if serving_scope is not None:
                scope_mode, scope_project_ids, selected_project_id = serving_scope
                emit_progress(
                    progress_cb,
                    "Build context",
                    "running",
                    "Planning purpose-sized read",
                )
                serving = try_tool_centric_read(
                    serving_conn,
                    question=user_text,
                    surface="telegram",
                    scope_mode=scope_mode,
                    project_ids=scope_project_ids,
                    as_of=datetime.now(timezone.utc),
                    trace_context=trace_context,
                    response_language=_read_response_language(serving_conn),
                    model_override=tool_centric_planner_model(),
                )
                if serving.served:
                    answer = str(serving.answer or "").strip()
                    emit_progress(
                        progress_cb,
                        "Build context",
                        "done",
                        "Purpose-sized read completed",
                    )
                    emit_progress(
                        progress_cb,
                        "Agent round 1",
                        "done",
                        "Typed result ready",
                    )
                    if on_stream_delta and answer:
                        on_stream_delta(None)
                        _simulate_stream(answer, on_stream_delta)
                    _persist_summary(
                        conn,
                        chat_id,
                        user_text,
                        answer,
                        skip_llm=True,
                    )
                    if selected_project_id:
                        _maybe_set_active_project(
                            chat_id,
                            selected_project_id,
                            conn,
                        )
                    trace_outcome.update({
                        "exit_reason": "tool_centric_typed_result",
                        "persistence_status": "completed",
                        "intent": "query",
                        "round_count": 0,
                        "tool_call_count": 1,
                        "guard_results": [],
                    })
                    return AgentChatResult(
                        reply=answer,
                        effective_project_id=selected_project_id,
                    )
        except Exception:
            logger.debug(
                "Tool-centric Telegram serving failed open to the broad Agent",
                exc_info=True,
            )
        finally:
            if read_scope is not None:
                read_scope.__exit__(None, None, None)

    if getattr(_defaults, "UNIFIED_LOOP_V2", False):
        from src.agent.unified_entry import run_telegram_loop

        emit_progress(progress_cb, "Build context", "running", "Loading context (unified v2)")
        v2_result, loop_result, effective_project_id, intent = run_telegram_loop(
            conn,
            user_text,
            chat_id,
            build_messages_fn=_build_initial_messages,
            max_rounds=max_rounds,
            progress_cb=progress_cb,
            on_stream_delta=on_stream_delta,
            required_tools_before_reply=required_tools_before_reply,
            operator_id=operator_id,
            trace_context=trace_context,
            trace_finish_owned_by_caller=True,
        )
        emit_progress(progress_cb, "Build context", "done", "Context ready")
        v2_result = _apply_final_telegram_advisory(
            conn,
            user_text=user_text,
            effective_project_id=effective_project_id,
            response_text=v2_result,
            loop_result=loop_result,
        )
        if on_stream_delta and v2_result and not loop_result.answer_streamed:
            on_stream_delta(None)
            _simulate_stream(v2_result, on_stream_delta)
        total_tool_calls = sum(len(rnd) for rnd in loop_result.tool_history)
        if not analysis_only and intent not in _SKIP_BG_INTENTS:
            _flush_memories(
                conn,
                chat_id,
                loop_result.messages,
                loop_result.round_count,
                total_tool_calls,
                project_id=effective_project_id,
            )
        if not analysis_only:
            _persist_summary(
                conn,
                chat_id,
                user_text,
                v2_result,
                skip_llm=(intent in _SKIP_BG_INTENTS),
            )
            if effective_project_id:
                _maybe_set_active_project(chat_id, effective_project_id, conn)
        trace_outcome.update({
            "exit_reason": loop_result.exit_reason,
            "persistence_status": "completed",
            "intent": intent,
            "round_count": loop_result.round_count,
            "tool_call_count": total_tool_calls,
            "guard_results": loop_result.guard_results,
        })
        return AgentChatResult(
            reply=v2_result,
            effective_project_id=effective_project_id,
        )

    # P1-6: for general knowledge queries, require wiki_search before the
    # model's final answer so domain/FIX facts come from the wiki SSOT, not
    # parametric memory. Only when the caller didn't explicitly set a
    # requirement (eval sets its own). Uses the same classifier as the post-
    # loop wiki guard, so status queries are not false-triggered.
    if required_tools_before_reply is None and intent == "general":
        from src.agent.guards import is_general_knowledge_query

        if is_general_knowledge_query(user_text):
            required_tools_before_reply = frozenset({"wiki_search"})

    # Late shortcut routing is shared with the pre-model closing interception.
    command = None
    if not analysis_only:
        from src.telegram.deterministic_commands import try_post_context_command

        command = try_post_context_command(conn, user_text, chat_id)
    if command is not None:
        return _present_deterministic_command(
            conn, user_text, chat_id, command,
            progress_cb=progress_cb, trace_context=trace_context,
            trace_outcome=trace_outcome,
        )

    emit_progress(progress_cb, "Build context", "running", "Loading context and conversation history")
    messages, effective_project_id, context_frame = _build_initial_messages(
        conn, user_text, chat_id, intent=intent, operator_id=operator_id,
    )

    from src.agent.memory_loop_helpers import context_operator_id

    prepared = prepare_loop(
        conn,
        user_text,
        effective_project_id=effective_project_id,
        chat_id=chat_id,
        max_rounds_override=max_rounds,
        surface="chat",
        operator_id=operator_id if operator_id is not None else context_operator_id(conn, chat_id=chat_id),
    )
    intent = prepared.intent
    trace_outcome["intent"] = intent
    tools = prepared.tools
    blocked = prepared.blocked_tool_names
    messages.extend(prepared.extra_messages)
    rounds = prepared.max_rounds

    from src.observability.lifecycle import (
        context_composition_metadata, emit_event, tool_contract_metadata,
    )

    emit_event(
        trace_context,
        "context.composed",
        metadata={
            **context_composition_metadata(
                messages,
                profile="telegram-legacy",
                context_frame=context_frame,
            ),
            **tool_contract_metadata(tools),
            "surface": "chat",
            "task": "telegram",
            "chat_id": chat_id,
            "project_id": effective_project_id,
            "intent": intent,
            "tool_names": [
                str(tool.get("function", {}).get("name") or "")
                for tool in tools
                if tool.get("function", {}).get("name")
            ],
        },
        payload={"ordered_messages": messages},
    )

    if intent == "query":
        logger.debug("Intent=query: using read-only toolset (%d tools)", len(tools))
    elif intent == "email":
        logger.debug("Intent=email: using email toolset (%d tools)", len(tools))
    elif intent == "orchestrator":
        logger.debug("Intent=orchestrator: using host toolset (%d tools)", len(tools))
    else:
        logger.debug("Intent=%s: using toolset (%d tools)", intent, len(tools))
    emit_progress(progress_cb, "Build context", "done", "Context ready")


    from src.agent.core_loop import run_agent_loop
    from src.agent.loop_config import LoopConfig
    from src.agent.memory_loop_helpers import loop_memory_kwargs
    from src.agent.round_context import (
        CompositeRoundContext,
        DbDriftProvider,
        TelegramNotificationsProvider,
    )
    from src.repositories.operator_state_repo import get_thinking_window_enabled

    show_thinking = False
    if conn is not None:
        show_thinking = get_thinking_window_enabled(conn)
    else:
        try:
            with _scoped_read_connection() as rconn:
                show_thinking = get_thinking_window_enabled(rconn)
        except sqlite3.OperationalError:
            logger.debug("Thinking-window preference unavailable", exc_info=True)

    if required_tools_before_reply is None:
        merged_req: set[str] = set()
    else:
        merged_req = set(required_tools_before_reply)
    from src.agent.prepare_loop import _progress_guard_active
    from src.agent.progress_advisory import progress_required_tools

    if _progress_guard_active(prepared.extra_messages):
        merged_req |= progress_required_tools(conn)
    if merged_req:
        required_tools_before_reply = frozenset(merged_req)

    from src.agent.memory_loop_helpers import resolve_loop_operator_id
    from src.agent.remember_parser import try_persist_remember_forbidden

    if not analysis_only:
        try_persist_remember_forbidden(
            conn, user_text, operator_id=operator_id if operator_id is not None else resolve_loop_operator_id(conn, chat_id=chat_id),
        )

    scope_forced = bool(
        _detect_project_id(user_text)
        or (
            context_frame is not None
            and getattr(context_frame, "_project_scope_forced", False)
        )
    )
    config = LoopConfig(
        caller_name="telegram",
        surface="chat",
        tools=tools,
        intent=intent,
        messages=messages,
        context_frame=context_frame,
        exposure_scopes=(
            *((context_frame.memory_exposure_scope(
                operator_id=operator_id if operator_id is not None else resolve_loop_operator_id(conn, chat_id=chat_id),
                surface="chat", task_type="chat",
                project_id=effective_project_id,
            ),) if context_frame is not None else ()),
            *getattr(prepared, "exposure_scopes", ()),
        ),
        max_rounds=rounds,
        effective_project_id=effective_project_id,
        project_scope_forced=scope_forced,
        project_scope_typo_corrected=bool(
            context_frame is not None
            and getattr(context_frame, "_project_scope_typo_corrected", False)
        ),
        conn=conn,
        chat_id=chat_id,
        round_context_provider=CompositeRoundContext([
            TelegramNotificationsProvider(drain_notifications),
            DbDriftProvider(),
        ]),
        loop_start_utc=datetime.now(timezone.utc),
        blocked_tool_names=blocked,
        show_thinking_window=show_thinking,
        on_stream_delta=on_stream_delta,
        progress_cb=progress_cb,
        required_tools_before_reply=required_tools_before_reply,
        digest_tool_results=(intent == "orchestrator"),
        trace_context=trace_context,
        **loop_memory_kwargs(conn, chat_id=chat_id, operator_id=operator_id),
    )
    loop_result = run_agent_loop(config)

    from src.agent.post_loop import apply_shared_post_loop

    result = apply_shared_post_loop(
        conn,
        user_text,
        loop_result,
        effective_project_id=effective_project_id,
        context_frame=context_frame,
        chat_id=chat_id,
    )

    # ── Telegram post-loop processing ─────────────────────────────────

    # Advisory suggestions are the final Telegram-only transform. Re-run the
    # shared grounded finalizer afterward so no appended text bypasses guards.
    result = _apply_final_telegram_advisory(
        conn,
        user_text=user_text,
        effective_project_id=effective_project_id,
        response_text=result,
        loop_result=loop_result,
    )

    # Fallback reveal when the loop exited without a live-streamed final answer
    # (exhaustion, claim violation, LLM error fallback, etc.).
    if on_stream_delta and result and not loop_result.answer_streamed:
        on_stream_delta(None)
        _simulate_stream(result, on_stream_delta)

    # Memory flush + session persistence (skip background LLM for read-only / casual intents)
    total_tool_calls = sum(len(rnd) for rnd in loop_result.tool_history)
    if not analysis_only:
        if intent not in _SKIP_BG_INTENTS:
            _flush_memories(conn, chat_id, loop_result.messages, loop_result.round_count, total_tool_calls,
                            project_id=effective_project_id)
        _persist_summary(
            conn, chat_id, user_text, result,
            skip_llm=(intent in _SKIP_BG_INTENTS),
        )
        if effective_project_id:
            _maybe_set_active_project(chat_id, effective_project_id, conn)
    trace_outcome.update({
        "exit_reason": loop_result.exit_reason,
        "persistence_status": "completed",
        "intent": intent,
        "round_count": loop_result.round_count,
        "tool_call_count": total_tool_calls,
        "guard_results": loop_result.guard_results,
    })
    return AgentChatResult(reply=result, effective_project_id=effective_project_id)


# ── Backward-compatible alias ───────────────────────────────────────────

def agent_respond(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    *,
    max_rounds: int | None = None,
    progress_cb: ProgressCallback | None = None,
    operator_id: str | None = None,
) -> str:
    """String-only wrapper: calls ``agent_loop``."""
    return agent_loop(
        conn,
        user_text,
        chat_id,
        max_rounds=max_rounds,
        progress_cb=progress_cb,
        operator_id=operator_id,
    ).reply
