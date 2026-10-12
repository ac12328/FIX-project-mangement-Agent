"""Production-shaped, side-effect-isolated baseline for email project truth."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from src.agent.loop_config import LoopConfig
from src.agent.specialists.email_runner import build_email_specialist_messages
from src.email.project_truth.contracts import (
    BaselineEmailRecommendation,
    EmailProjectTruthCase,
    PipelineCallMetadata,
)
from src.llm.client import get_last_completed_model, get_trace_ids, reset_trace_context
from src.email.project_truth.model_routing import normalize_research_model
from src.tools.chat_handlers import (
    clear_email_analysis_result,
    get_email_analysis_result,
)
from src.tools.registry import get_openai_tools_by_name

BASELINE_TOOL_NAMES = frozenset({
    "load_project_context",
    "wiki_search",
    "get_project_status",
    "report_email_analysis",
})


def baseline_tools() -> list[dict[str, Any]]:
    """Return the exact read/report-only baseline tool bundle."""
    return get_openai_tools_by_name(BASELINE_TOOL_NAMES)


def build_bounded_inbound_text(case: EmailProjectTruthCase) -> str:
    """Render only the frozen case evidence into the production inbound envelope."""
    lines = [
        "[INBOUND EMAIL]",
        f"Project: {case.project_id}",
        f"Analysis ref: {case.source_ref}",
        f"From: {case.sender_email}",
        f"Subject: {case.subject}",
        "",
        case.newest_authored_body,
    ]
    attachments = [source for source in case.sources if source.source_role == "attachment"]
    if attachments:
        lines.extend(["", "<attachments>"])
        for index, source in enumerate(attachments, start=1):
            lines.extend([
                f'  <extracted-text filename="bounded-attachment-{index}.txt" source_ref="{source.source_ref}">',
                source.text,
                "  </extracted-text>",
            ])
        lines.append("</attachments>")
    return "\n".join(lines)


@contextmanager
def _model_override(model_override: str | None) -> Iterator[None]:
    """Force the research model and suppress production error-card side effects."""
    import os
    import src.agent.core_loop as core_loop
    import src.tools.executor as executor

    normalized_model = normalize_research_model(model_override)
    original = core_loop.llm_call_with_tools
    original_wiki_handler = executor.TOOL_HANDLERS.get("wiki_search")

    def research_call(task, messages, tools, temperature=0.3, **kwargs):
        if normalized_model:
            kwargs["model_override"] = normalized_model
        return original(task, messages, tools, temperature=temperature, **kwargs)

    def research_wiki_search(_conn, arguments):
        from src.wiki.indexer import wiki_search

        query = arguments.get("query", "")
        top_k = arguments.get("top_k", 5)
        domain = arguments.get("domain")
        return {
            "results": wiki_search(query, top_k=top_k, domain_filter=domain),
            "query": query,
        }

    previous_disable = os.environ.get("AGENTFIXB_DISABLE_LLM_ERROR_CARDS")
    os.environ["AGENTFIXB_DISABLE_LLM_ERROR_CARDS"] = "1"
    core_loop.llm_call_with_tools = research_call
    executor.TOOL_HANDLERS["wiki_search"] = research_wiki_search
    try:
        yield
    finally:
        core_loop.llm_call_with_tools = original
        if original_wiki_handler is None:
            executor.TOOL_HANDLERS.pop("wiki_search", None)
        else:
            executor.TOOL_HANDLERS["wiki_search"] = original_wiki_handler
        if previous_disable is None:
            os.environ.pop("AGENTFIXB_DISABLE_LLM_ERROR_CARDS", None)
        else:
            os.environ["AGENTFIXB_DISABLE_LLM_ERROR_CARDS"] = previous_disable


def ensure_baseline_success(exit_reason: str) -> None:
    """Turn a loop-level provider failure into a failed research stage."""
    if exit_reason == "llm_error":
        raise RuntimeError("baseline email specialist exited with llm_error")


def _render_stored_result(stored: dict[str, Any] | None, fallback: str) -> str:
    if not stored:
        return fallback
    lines = [stored.get("summary") or fallback or "No summary was reported."]
    proposed = stored.get("proposed_fields") or {}
    if proposed:
        lines.append("Recommended changes:")
        lines.extend(f"- {field}: {value}" for field, value in proposed.items())
    action = stored.get("suggested_action")
    if action:
        lines.append(f"Suggested action: {action}")
    detail = stored.get("assessment_detail") or {}
    citations: list[tuple[str, str]] = []
    for fact in detail.get("extracted_facts") or detail.get("facts") or []:
        for citation in fact.get("citations") or []:
            ref = str(citation.get("source_ref") or "")
            quote = str(citation.get("quote") or "")
            if ref and quote and (ref, quote) not in citations:
                citations.append((ref, quote))
    if citations:
        lines.append("Source evidence:")
        lines.extend(f'- [{ref}] "{quote}"' for ref, quote in citations)
    return "\n".join(lines)


def run_baseline(
    conn: sqlite3.Connection,
    case: EmailProjectTruthCase,
    *,
    model_override: str | None = None,
    max_rounds: int = 5,
) -> BaselineEmailRecommendation:
    """Run the current email specialist against a disposable case database."""
    from src.agent.core_loop import run_agent_loop

    user_text = build_bounded_inbound_text(case)
    clear_email_analysis_result(case.project_id, case.source_ref)
    reset_trace_context()
    frame, messages, effective_pid = build_email_specialist_messages(
        conn, user_text, project_id=case.project_id,
    )
    tools = baseline_tools()
    started = time.perf_counter()
    try:
        with _model_override(model_override):
            loop_result = run_agent_loop(LoopConfig(
                caller_name="research:email-project-truth-baseline",
                surface="tool",
                tools=tools,
                intent="email",
                messages=messages,
                context_frame=frame,
                max_rounds=max_rounds,
                effective_project_id=effective_pid,
                project_scope_forced=True,
                conn=conn,
                enable_compression=False,
                refresh_anchors_each_round=False,
                enable_fabrication_guard=False,
                chat_id=0,
                thread_id=f"email-project-truth:{case.case_id}",
                runtime_allowed_tool_names=BASELINE_TOOL_NAMES,
                blocked_tool_names=frozenset({"manage_followup"}),
                execution_context={"research_mode": True, "side_effects_forbidden": True},
            ))
        latency_ms = int((time.perf_counter() - started) * 1000)
        ensure_baseline_success(loop_result.exit_reason)
        stored = get_email_analysis_result(case.project_id, case.source_ref)
        trace_ids = get_trace_ids()
        proposed = dict((stored or {}).get("proposed_fields") or {})
        return BaselineEmailRecommendation(
            stored_result=stored,
            proposed_fields={str(key): str(value) for key, value in proposed.items()},
            summary=str((stored or {}).get("summary") or loop_result.reply or ""),
            suggested_action=str((stored or {}).get("suggested_action") or ""),
            answer=_render_stored_result(stored, loop_result.reply or ""),
            reply=loop_result.reply or "",
            round_count=loop_result.round_count,
            tool_history=[list(items) for items in loop_result.tool_history],
            guard_results=list(loop_result.guard_results),
            exit_reason=loop_result.exit_reason,
            call_metadata=PipelineCallMetadata(
                model=get_last_completed_model() or normalize_research_model(model_override) or "",
                latency_ms=latency_ms,
                trace_ids=trace_ids,
                attempts=max(1, loop_result.round_count),
            ),
        )
    finally:
        clear_email_analysis_result(case.project_id, case.source_ref)
