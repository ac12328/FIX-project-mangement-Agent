"""Lean email analysis harness — PR9 replacement for fat agent_chat email path."""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass

from config.settings import BASE_DIR
from src.agent.core_loop import run_agent_loop
from src.agent.loop_config import LoopConfig
from src.agent.specialists.contracts import SpecialistType
from src.agent.specialists.tools import get_tools_for_specialist
from src.context.engine import ContextEngine
from src.models.context_frame import ContextFrame
from src.observability.contracts import (
    TraceContext,
    current_trace_context,
    trace_context_scope,
)
from src.observability.lifecycle import (
    context_composition_metadata,
    emit_event,
    emit_run_finish,
    emit_run_start,
    tool_contract_metadata,
)
from src.tools.chat_handlers import get_email_analysis_result
from src.tools.registry import get_tools_for_intent
from src.utils.operator_language import (
    operator_language_instruction,
    read_response_language,
    summary_action_language_rule,
)

logger = logging.getLogger(__name__)

_EMAIL_MAX_ROUNDS = 5


@dataclass
class EmailAgentResult:
    reply: str
    trace_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None


def _extract_project_id(user_text: str) -> str | None:
    match = re.search(r"^Project:\s*(\S+)", user_text, re.MULTILINE | re.IGNORECASE)
    return match.group(1) if match else None


def _extract_analysis_ref(user_text: str) -> str | None:
    match = re.search(r"^Analysis ref:\s*(\S+)", user_text, re.MULTILINE | re.IGNORECASE)
    return match.group(1) if match else None


def _sender_trust_instructions(user_text: str) -> str:
    from src.email.sender_trust import (
        build_sender_trust_context,
        extract_sender_from_inbound_text,
    )

    return build_sender_trust_context(extract_sender_from_inbound_text(user_text))


def build_email_specialist_messages(
    conn: sqlite3.Connection | None,
    user_text: str,
    project_id: str | None = None,
) -> tuple[ContextFrame, list[dict], str | None]:
    """Bootstrap email context: instructions + L1 project truth + language policy."""
    pid = project_id or _extract_project_id(user_text)
    instructions_path = BASE_DIR / "prompts" / "email_analysis_instructions.md"
    specialist_path = BASE_DIR / "prompts" / "specialists" / "email.md"
    parts = []
    if specialist_path.exists():
        parts.append(specialist_path.read_text(encoding="utf-8").strip())
    if instructions_path.exists():
        parts.append(instructions_path.read_text(encoding="utf-8").strip())
    # Share the exact contract vocabulary used by the evidence extractor and
    # deterministic evaluator. Do not maintain a parallel predicate list here.
    import json
    from src.email.project_truth.extraction import milestone_contract_payload
    try:
        contract = milestone_contract_payload()
    except Exception:
        logger.exception("Milestone extraction contract unavailable")
        parts.append("The milestone contract is unavailable. Do not propose project-state changes; report that policy validation is unavailable.")
    else:
        parts.append(
            "## Authoritative extraction vocabulary\n"
            "For milestone facts, use an exact allowed_fact_predicates value from the contract below. "
            "Do not emit semantic_criterion_predicate as an observed fact unless it is also allowed. "
            "Extract each explicit requested action separately using explicit_obligation, with subject "
            "internal/external/shared and a faithful concrete action in value. A request can be confirmed "
            "without the requested work being completed. Keep plans, conditions, negative evidence and "
            "quoted history distinct. Do not invent an authority merely to match a predicate. "
            "Use the supplied policy_version for assessment_detail.\n"
            + json.dumps(contract, ensure_ascii=True)
        )
    system = "\n\n".join(parts) or "Analyze inbound email and call report_email_analysis."

    lang = read_response_language(conn)
    lang_policy = operator_language_instruction(lang)
    summary_rule = summary_action_language_rule(lang)
    system = f"{system}\n\n## {lang_policy}\n\n## Summary language\n{summary_rule}"

    project_truth = ""
    missing_flags: list[str] = []
    confidence = 1.0
    if pid and conn is not None:
        project_truth, missing_flags, confidence = ContextEngine(conn)._load_l1_snapshot(pid)

    frame = ContextFrame(
        identity=system,
        project_truth=project_truth,
        instructions=_sender_trust_instructions(user_text),
        missing_flags=missing_flags,
        context_confidence=confidence,
    )
    messages = frame.to_messages()
    # Trailing reminder so language policy wins over any template hardcoding.
    messages.append({"role": "system", "content": f"{lang_policy}\n\n{summary_rule}"})
    messages.append({"role": "user", "content": user_text})
    return frame, messages, pid


def run_email_specialist_loop(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    *,
    max_rounds: int = _EMAIL_MAX_ROUNDS,
    project_id: str | None = None,
    trace_context: TraceContext | None = None,
) -> EmailAgentResult:
    """Run the email specialist with explicit lifecycle ownership."""
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
            emit_run_start(
                context,
                metadata={
                    "surface": "email",
                    "task": "email",
                    "session_id": str(chat_id),
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "max_rounds": max_rounds,
                    "entrypoint": "run_email_specialist_loop",
                },
                payload={"user_input": user_text},
            )
        emit_event(
            context,
            "email.start",
            status="running",
            metadata={
                "chat_id": chat_id,
                "project_id": project_id,
                "max_rounds": max_rounds,
            },
            payload={"user_input": user_text},
        )
        try:
            result, analysis_observed = _run_email_specialist_loop_body(
                conn,
                user_text,
                chat_id,
                max_rounds=max_rounds,
                project_id=project_id,
                trace_context=context,
            )
            result.trace_id = context.trace_id
            result.run_id = context.run_id
            result.turn_id = context.turn_id
            emit_event(
                context,
                "email.end",
                metadata={
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "analysis_result_observed": analysis_observed,
                    "persistence_status": (
                        "completed" if analysis_observed else "not_observed"
                    ),
                },
                payload={"reply": result.reply},
            )
            if owns_context:
                emit_run_finish(
                    context,
                    status="completed",
                    exit_reason="completed",
                    final_output=result.reply,
                    persistence_status=(
                        "completed" if analysis_observed else "not_observed"
                    ),
                    metadata={
                        "chat_id": chat_id,
                        "project_id": project_id,
                        "analysis_result_observed": analysis_observed,
                    },
                )
            return result
        except Exception as exc:
            emit_event(
                context,
                "email.error",
                status="error",
                metadata={
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "error_class": type(exc).__name__,
                },
                payload={"error": str(exc)},
            )
            if owns_context:
                emit_run_finish(
                    context,
                    status="error",
                    exit_reason=type(exc).__name__,
                    persistence_status="error",
                    anomaly_flags=["error"],
                    metadata={
                        "chat_id": chat_id,
                        "project_id": project_id,
                        "error_class": type(exc).__name__,
                    },
                )
            raise


def _run_email_specialist_loop_body(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
    *,
    max_rounds: int,
    project_id: str | None,
    trace_context: TraceContext,
) -> tuple[EmailAgentResult, bool]:
    """Build email context, execute the loop, then observe persisted analysis."""
    from src.db.connection import scoped_read

    def _run_with(
        rconn: sqlite3.Connection | None,
    ) -> tuple[EmailAgentResult, bool]:
        frame, messages, effective_pid = build_email_specialist_messages(
            rconn, user_text, project_id=project_id,
        )
        tools = get_tools_for_specialist(SpecialistType.EMAIL)
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
                    profile="email-specialist",
                    context_frame=frame,
                ),
                **tool_contract_metadata(tools),
                "surface": "email",
                "task": "email",
                "chat_id": chat_id,
                "project_id": effective_pid,
                "tool_names": tool_names,
            },
            payload={"ordered_messages": messages},
        )
        config = LoopConfig(
            caller_name="specialist:email",
            surface="tool",
            tools=tools,
            intent="email",
            messages=messages,
            context_frame=frame,
            max_rounds=max_rounds,
            effective_project_id=effective_pid,
            project_scope_forced=bool(effective_pid),
            # Self-scoped tools; rconn may be query_only (scoped_read).
            conn=None,
            enable_compression=False,
            refresh_anchors_each_round=False,
            enable_fabrication_guard=False,
            chat_id=chat_id,
            thread_id=str(chat_id),
            trace_context=trace_context,
        )
        loop_result = run_agent_loop(config)
        stored = get_email_analysis_result(
            project_id=effective_pid,
            analysis_ref=_extract_analysis_ref(user_text),
        )
        if stored and stored.get("summary"):
            fields = list((stored.get("proposed_fields") or {}).keys())
            lang = read_response_language(rconn)
            pid = stored.get("project_id", effective_pid or "")
            summary = stored["summary"]
            if lang == "en":
                reply = f"? Analyzed email for {pid}. {summary}"
                if fields:
                    reply += f" (proposed updates: {', '.join(fields)})"
            else:
                reply = f"? ??? {pid} ???{summary}"
                if fields:
                    reply += f"??????{', '.join(fields)}?"
            return EmailAgentResult(reply=reply), True
        return EmailAgentResult(reply=loop_result.reply or ""), False

    if conn is not None:
        return _run_with(conn)
    with scoped_read() as rconn:
        return _run_with(rconn)


def should_use_email_specialist() -> bool:
    from config import defaults
    return getattr(defaults, "EMAIL_SPECIALIST_MODE", False)


def email_tool_count() -> int:
    return len(get_tools_for_intent("email"))
