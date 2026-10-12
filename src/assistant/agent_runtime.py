"""Unified-loop runtime adapter for the AI Workspace surface."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from opentelemetry import trace

from src.agent.grounded_finalizer import finalize_grounded_reply
from src.agent.unified_entry import HarnessProfile, LoopRequest, run_surface_loop
from src.assistant.exact_read_shadow import (
    classify_exact_read_shadow,
    observe_exact_read_shadow,
)
from src.assistant.fast_path_shadow import classify_fast_path_shadow
from src.assistant.grounding import ground_workspace_answer
from src.assistant.receipt_hooks import WorkspaceReceiptHooks
from src.context.engine import ContextEngine
from src.observability.contracts import TraceContext, trace_context_scope
from src.observability.lifecycle import emit_run_finish, emit_run_start
from src.observability.progress import emit_progress
from src.tools.registry import get_openai_tools_by_name
from src.tools.role_tools import AgentRole
from src.utils.operator_language import (
    operator_language_instruction,
    read_response_language,
)

_WORKSPACE_TOOL_NAMES = frozenset({
    "portfolio_attention",
    "search_project_parties",
    "query_portfolio",
    "get_project_brief",
    "search_project_timeline",
    "list_project_work",
    "list_unanswered_threads",
    "get_project_evidence_audit",
    "assess_milestone_readiness",
    "delegate",
})

_WORKSPACE_POLICY = """
You are the AI Workspace portfolio agent. Understand the request, call the relevant backend tools,
and answer directly from their returned records. Never answer a portfolio or project factual question
from model memory alone.

Tool routing:
- portfolio_attention: attention, aging, priority, risk, overdue, or "this week" questions.
- search_project_parties: vendor, organisation, client, regulator, or counterparty relationships.
- query_portfolio: project lists and exact status/milestone/ball/market/client filters.
- get_project_brief: one project's status, owner/ball, recent changes, emails, evidence, and next action.
- search_project_timeline: chronological emails, activities, milestones, and evidence for one project.
- list_project_work: commitments, follow-ups, internal actions, due dates, and residual work.
- list_unanswered_threads: inbound-last email threads with no newer outbound response. For portfolio-level PM
  triage phrased as "still waiting on our reply" or equivalent, use include_terminal=false and the conservative
  min_age_hours=24 window unless the user explicitly asks for a different age threshold.
- get_project_evidence_audit: authoritative truth, accepted/rejected/superseded updates, and conflicts.
- assess_milestone_readiness: deterministic source-bound milestone criteria assessment.
- delegate: internal FIX onboarding or operating-manual knowledge. Delegate to skill_id="research"; the research specialist must use wiki_search and return cited Wiki evidence.
- web_search: external facts only when this tool is available. Never imply web search ran otherwise.

Grounding contract:
- Every factual sentence or table summary must cite one or more exact source IDs returned by tools.
- State the actual Workspace snapshot/as-of time when answering current, aging, latest, or historical questions.
- "All projects", "check all", and "entire portfolio" include every visible terminal project and all result pages.
- Citation syntax is [[source:<source_id>]]. Do not invent or alter source IDs.
- The current project record is authoritative for current state. Emails and activity are historical evidence.
- Never infer when a project entered a status from projects.updated_at; updated_at is not a status-transition timestamp.
- Historical project-status transitions must come from accepted proposed_updates audit records. Use query_portfolio.status_transition when available; otherwise state that the transition date cannot be verified.
- Treat get_project_evidence_audit.conflicts as authoritative for present evidence conflicts. If it is empty, do not
  claim a current conflict merely because older email or activity records differ from the current row.
- Accepted operator updates and current fact/manual-note evidence override superseded, rejected, or undone email updates.
- A contact-only, planning, or suggested-action message is not completion evidence unless it explicitly proves it.
- Clearly label recommendations and proposed transitions; never describe them as completed changes.
- Answer the requested status, comparison, or next action first. Avoid dumping unrelated readiness criteria,
  audit totals, historical phases, or additional tool output unless it materially answers the question.
- Keep the default answer concise (normally under 900 words). Structured Workspace tables render tool rows separately,
  so do not reproduce the same rows in a Markdown table. Give the exact count, scope/filter summary, and material
  conclusions in prose; let the structured result table carry the row-by-row records.
- For unanswered-thread scans, state the age/status filters, count and project coverage, then give a compact
  per-project summary of each materially distinct recent request from its subject/body. Do not emit a source-ID
  inventory or evidence-distribution table: a project name plus citations does not describe what is awaiting reply.
  Prioritize actionable FIX/onboarding threads over notification-only mail, while retaining the response-candidate caveat.
- When a relevant email contains multiple requests, exceptions, or explicit rationales, cover each material item;
  do not stop after the first exemption or request.
- When the user contrasts project completion with outstanding work, lead with current status and the live blocker.
  Omit completed historical phases unless they are needed to explain the current blocker. Never summarize heartbeat,
  logon, failover, or one accepted test message as "all tests passed" or project completion.
- When the project row has a current next_action, do not restate an older activity-log next_action as another current
  action. Historical actions are superseded context and should normally be omitted unless history was requested.
- If authoritative records conflict, state the conflict and give the safest evidence-based next action.
- Do not expose chain-of-thought, hidden reasoning, raw tool protocol, or internal marker names.
""".strip()


@dataclass
class WorkspaceAgentResult:
    text: str
    parts: list[dict[str, Any]]
    tool_names: tuple[str, ...]
    validation_errors: tuple[str, ...] = ()
    tool_results: tuple[dict[str, Any], ...] = ()
    trace_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None


def run_workspace_agent(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    thread_id: str,
    turn_id: str | None = None,
    message: str,
    project_ids: list[str],
    scope_mode: str,
    web_enabled: bool,
    progress_cb=None,
    as_of: datetime | None = None,
    routing_mode: Literal["production", "autonomy_eval"] = "production",
    can_write: bool = False,
    post_commit: list[Callable[[], None]] | None = None,
    receipt_hooks: WorkspaceReceiptHooks | None = None,
) -> WorkspaceAgentResult:
    """Execute and observe one natural-language Workspace agent turn."""
    tracer = trace.get_tracer("agentfixbpro.assistant")
    with tracer.start_as_current_span("assistant.agent") as span:
        span.set_attribute("assistant.scope_mode", scope_mode)
        span.set_attribute("assistant.project_count", len(project_ids))
        span.set_attribute("assistant.web_enabled", web_enabled)
        span.set_attribute("assistant.routing_mode", routing_mode)
        span.set_attribute("assistant.can_write", can_write)
        span.set_attribute("assistant.request_chars", len(message))
        result = _run_workspace_agent(
            conn,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            turn_id=turn_id,
            message=message,
            project_ids=project_ids,
            scope_mode=scope_mode,
            web_enabled=web_enabled,
            progress_cb=progress_cb,
            as_of=as_of,
            routing_mode=routing_mode,
            can_write=can_write,
            post_commit=post_commit,
            receipt_hooks=receipt_hooks,
        )
        span.set_attribute("assistant.output_chars", len(result.text))
        span.set_attribute("assistant.part_count", len(result.parts))
        span.set_attribute("assistant.tool_count", len(result.tool_names))
        span.set_attribute("assistant.validation_error_count", len(result.validation_errors))
        return result


def _run_workspace_agent(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    thread_id: str,
    turn_id: str | None = None,
    message: str,
    project_ids: list[str],
    scope_mode: str,
    web_enabled: bool,
    progress_cb=None,
    as_of: datetime | None = None,
    routing_mode: Literal["production", "autonomy_eval"] = "production",
    can_write: bool = False,
    post_commit: list[Callable[[], None]] | None = None,
    receipt_hooks: WorkspaceReceiptHooks | None = None,
) -> WorkspaceAgentResult:
    """Execute one Workspace turn through typed serving or the broad Agent loop."""
    if routing_mode not in {"production", "autonomy_eval"}:
        raise ValueError(f"Unsupported Workspace routing mode: {routing_mode}")

    current = as_of or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    trace_context = TraceContext.create(
        surface="web",
        task="workspace",
        thread_id=thread_id,
        turn_id=turn_id,
    )
    response_language = read_response_language(conn)
    language_policy = operator_language_instruction(response_language)
    tracer = trace.get_tracer("agentfixbpro.assistant")

    try:
        with trace_context_scope(trace_context):
            _safe_workspace_run_start(
                trace_context,
                metadata={
                    "surface": "web",
                    "task": "workspace",
                    "thread_id": thread_id,
                    "owner_user_id": owner_user_id,
                    "scope_mode": scope_mode,
                    "project_count": len(project_ids),
                    "project_ids": list(project_ids),
                    "project_id": project_ids[0] if len(project_ids) == 1 else None,
                    "web_enabled": web_enabled,
                    "routing_mode": routing_mode,
                    "can_write": can_write,
                    "entrypoint": "run_workspace_agent",
                },
                payload={"user_input": message},
            )

            if routing_mode == "production" and can_write:
                from src.project_reconciliation.surface import (
                    classify_reconciliation_request,
                    execute_reconciliation_request,
                    reconciliation_tool_trace,
                )
                from src.services.operator_project_update import (
                    classify_workspace_operator_write,
                )

                if classify_workspace_operator_write(
                    message,
                    scope_mode=scope_mode,
                    project_ids=list(project_ids or []),
                ) is not None:
                    reconciliation_request = None
                else:
                    reconciliation_request = classify_reconciliation_request(
                        message,
                        fallback_project_ids=(
                            project_ids if len(project_ids) == 1 else ()
                        ),
                    )
                if reconciliation_request is not None:
                    target_id = reconciliation_request.project_id
                    if (
                        scope_mode == "projects"
                        and target_id is not None
                        and target_id not in project_ids
                    ):
                        answer = (
                            f"**{target_id} \u4e0d\u5728\u76ee\u524d Workspace scope\u3002** "
                            "\u8acb\u5148\u628a\u5c08\u6848\u52a0\u5165\u76ee\u524d context\uff0c"
                            "\u6216\u5207\u63db\u5230 Portfolio scope\u3002"
                        )
                        _safe_workspace_run_finish(
                            trace_context,
                            status="completed",
                            exit_reason="project_reconciliation_scope_denied",
                            final_output=answer,
                            persistence_status="denied",
                            metadata={
                                "round_count": 0,
                                "tool_count": 0,
                                "project_id": target_id,
                            },
                        )
                        return WorkspaceAgentResult(
                            text=answer,
                            parts=[
                                {"type": "text", "text": answer},
                                {
                                    "type": "error",
                                    "code": "project_outside_workspace_scope",
                                    "message": answer,
                                },
                            ],
                            tool_names=(),
                            trace_id=trace_context.trace_id,
                            run_id=trace_context.run_id,
                            turn_id=trace_context.turn_id,
                        )

                    emit_progress(
                        progress_cb,
                        "Reconcile project",
                        "running",
                        "Freezing and evaluating historical email evidence",
                    )
                    effect_start = len(post_commit) if post_commit is not None else 0
                    execution = execute_reconciliation_request(
                        conn,
                        request=reconciliation_request,
                        operator_id=owner_user_id,
                        chat_id=thread_id,
                        trusted_surface="ai_workspace",
                        post_commit=post_commit,
                        allowed_project_ids=(project_ids if scope_mode == "projects" else None),
                        request_key=(
                            f"project-reconcile:workspace:{trace_context.turn_id}"
                            if trace_context.turn_id
                            else None
                        ),
                    )
                    answer = execution.text
                    tool_trace = reconciliation_tool_trace(execution)
                    emit_progress(
                        progress_cb,
                        "Reconcile project",
                        ("running" if execution.result.status == "pending_commit"
                         else "done" if execution.result.ok else "error"),
                        (
                            "DB receipt confirmed"
                            if execution.result.committed
                            else "Awaiting outer transaction commit"
                            if execution.result.status == "pending_commit"
                            else "Reconciliation receipt ready"
                        ),
                    )
                    parts = [{"type": "text", "text": answer}]
                    parts.extend(_tool_parts([tool_trace]))
                    if execution.result.status == "pending_commit":
                        parts[1]["status"] = "running"
                        parts[1]["text"] = "Awaiting outer DB commit"
                    result = WorkspaceAgentResult(
                        text=answer,
                        parts=parts,
                        tool_names=("project_reconcile",),
                        tool_results=(tool_trace,),
                        trace_id=trace_context.trace_id,
                        run_id=trace_context.run_id,
                        turn_id=trace_context.turn_id,
                    )

                    def finish_receipt(receipt_execution):
                        _safe_workspace_run_finish(
                            trace_context,
                            status="completed",
                            exit_reason="deterministic_project_reconciliation",
                            final_output=receipt_execution.text,
                            persistence_status=(
                                "committed" if receipt_execution.result.committed
                                else receipt_execution.result.status
                            ),
                            metadata={
                                "round_count": 0,
                                "tool_count": 1,
                                "project_id": receipt_execution.request.project_id,
                                "operation_id": receipt_execution.result.operation_id,
                                "committed": receipt_execution.result.committed,
                            },
                        )

                    if execution.result.status == "pending_commit" and post_commit is not None:
                        def confirm_stored_receipt() -> None:
                            from src.db.connection import scoped_read
                            from src.project_reconciliation.contracts import ReconciliationResult
                            from src.project_reconciliation.repository import get_operation
                            from src.project_reconciliation.surface import format_reconciliation_result

                            with scoped_read() as read_conn:
                                operation = get_operation(read_conn, execution.result.operation_id)
                            if operation is None or not operation.get("result"):
                                raise RuntimeError("Committed reconciliation receipt is missing")
                            receipt = ReconciliationResult.model_validate(operation["result"])
                            if not receipt.committed or receipt.operation_id != execution.result.operation_id:
                                raise RuntimeError("Reconciliation receipt was not durably committed")
                            confirmed = replace(
                                execution, result=receipt,
                                text=format_reconciliation_result(receipt),
                            )
                            confirmed_trace = reconciliation_tool_trace(confirmed)
                            result.text = confirmed.text
                            result.parts = [{"type": "text", "text": confirmed.text},
                                            *_tool_parts([confirmed_trace])]
                            result.tool_results = (confirmed_trace,)
                            finish_receipt(confirmed)

                        # Hydrate before publication callbacks; a notification failure cannot
                        # change the durable receipt or the completed Workspace answer.
                        if receipt_hooks is not None:
                            receipt_hooks.operation_id = execution.result.operation_id
                            receipt_hooks.verify_receipt = confirm_stored_receipt
                        else:
                            # Preserve direct list-based callers outside the API worker.
                            post_commit.insert(effect_start, confirm_stored_receipt)
                    else:
                        finish_receipt(execution)
                    return result

            if routing_mode == "production":
                from src.assistant.tool_centric_serving import (
                    should_attempt_tool_centric_serving,
                    tool_centric_planner_model,
                    try_tool_centric_read,
                )

                if should_attempt_tool_centric_serving("workspace"):
                    emit_progress(
                        progress_cb,
                        "Typed portfolio query",
                        "running",
                        "Planning a verified Workspace data query",
                    )
                    serving = try_tool_centric_read(
                        conn,
                        question=message,
                        surface="workspace",
                        scope_mode=scope_mode,
                        project_ids=project_ids,
                        as_of=current,
                        trace_context=trace_context,
                        response_language=response_language,
                        model_override=tool_centric_planner_model(),
                    )
                    if serving.served:
                        emit_progress(
                            progress_cb,
                            "Typed portfolio query",
                            "done",
                            "Verified Workspace data query complete",
                        )
                        answer = str(serving.answer or "").strip()
                        tool_trace = {
                            "tool": "portfolio_query_v2",
                            "arguments": dict(serving.query or {}),
                            "result": dict(serving.tool_result or {}),
                        }
                        parts: list[dict[str, Any]] = [
                            {"type": "text", "text": answer}
                        ]
                        parts.extend(_tool_parts([tool_trace]))
                        parts.extend(dict(item) for item in serving.sources)
                        _safe_workspace_run_finish(
                            trace_context,
                            status="completed",
                            exit_reason="tool_centric_typed_result",
                            final_output=answer,
                            persistence_status="not_requested",
                            metadata={
                                **serving.safe_metadata(),
                                "round_count": 0,
                                "tool_count": 1,
                                "project_count": len(project_ids),
                            },
                        )
                        return WorkspaceAgentResult(
                            text=answer,
                            parts=parts,
                            tool_names=("portfolio_query_v2",),
                            tool_results=(tool_trace,),
                            trace_id=trace_context.trace_id,
                            run_id=trace_context.run_id,
                            turn_id=trace_context.turn_id,
                        )

        # Broad context composition is deliberately deferred until direct serving
        # has declined the request. This is the main latency/token saving.
        with tracer.start_as_current_span("assistant.context.build") as context_span:
            history_rows = conn.execute(
                """SELECT role, content FROM assistant_messages
                   WHERE thread_id=? AND role IN ('user','assistant')
                   ORDER BY created_at DESC, message_id DESC LIMIT 24""",
                (thread_id,),
            ).fetchall()
            history = [dict(row) for row in reversed(history_rows)]
            if (
                history
                and history[-1].get("role") == "user"
                and history[-1].get("content") == message
            ):
                history.pop()

            engine = ContextEngine(conn)
            frame = engine.build_for_web_chat(
                message,
                project_ids=project_ids,
                conversation_history=history,
                profile="full",
                operator_id=owner_user_id,
                session_id=f"assistant:{thread_id}",
            )
            messages = frame.to_messages()
            if messages and messages[0].get("role") == "system":
                messages[0] = {
                    **messages[0],
                    "content": f"{messages[0].get('content') or ''}\n\n{language_policy}",
                }
            else:
                messages.insert(0, {"role": "system", "content": language_policy})
            messages.append({
                "role": "system",
                "content": f"{_WORKSPACE_POLICY}\n\n{language_policy}",
            })
            messages.append({"role": "user", "content": message})
            from src.llm.client import _estimate_tokens

            context_span.set_attribute("assistant.history_count", len(history))
            context_span.set_attribute("assistant.project_count", len(project_ids))
            context_span.set_attribute("assistant.scope_mode", scope_mode)
            context_span.set_attribute("assistant.context_message_count", len(messages))
            context_span.set_attribute("assistant.estimated_tokens", _estimate_tokens(messages))

        source_sink: list[dict[str, Any]] = []
        tool_result_sink: list[dict[str, Any]] = []
        tool_names = set(_WORKSPACE_TOOL_NAMES)
        if web_enabled:
            tool_names.add("web_search")
        tools = get_openai_tools_by_name(frozenset(tool_names))
        classified_required_tools = _required_tools_for_request(
            message,
            project_ids=project_ids,
            scope_mode=scope_mode,
        )
        required_tools = (
            frozenset()
            if routing_mode == "autonomy_eval"
            else classified_required_tools
        )
        fast_path_shadow = classify_fast_path_shadow(
            message,
            required_tools=classified_required_tools,
            scope_mode=scope_mode,
            project_count=len(project_ids),
            web_enabled=web_enabled,
        )
        exact_read_shadow = classify_exact_read_shadow(
            message,
            required_tools=classified_required_tools,
            scope_mode=scope_mode,
            web_enabled=web_enabled,
        )
        all_projects_requested = bool(re.search(
            r"\b(?:all (?:the )?projects?|every project|check all|entire portfolio|full portfolio|whole portfolio)\b",
            message,
            re.IGNORECASE,
        ))
        request = LoopRequest(
            surface="web",
            user_text=message,
            role=AgentRole.QUERY,
            project_id=project_ids[0] if len(project_ids) == 1 else None,
            harness=HarnessProfile(digest_tool_results=False, context_profile="full"),
            max_rounds=6,
            progress_cb=progress_cb,
            operator_id=owner_user_id,
            thread_id=thread_id,
            required_tools_before_reply=required_tools,
            apply_shared_post_processing=False,
            trace_context=trace_context,
            trace_finish_owned_by_caller=True,
            pre_loop_trace_events=({
                "event_type": "routing.fast_path.shadow",
                "status": "ok",
                "metadata": {
                    **fast_path_shadow.as_metadata(),
                    "scope_mode": scope_mode,
                    "project_count": len(project_ids),
                    "required_tools": sorted(classified_required_tools),
                    "web_enabled": web_enabled,
                    "routing_mode": routing_mode,
                },
            },),
            execution_context={
                "read_only_execution": True,
                "assistant_scope_mode": scope_mode,
                "assistant_scope_project_ids": list(project_ids),
                "assistant_source_sink": source_sink,
                "assistant_tool_result_sink": tool_result_sink,
                "assistant_web_enabled": web_enabled,
                "assistant_as_of": current.isoformat(),
                "assistant_all_projects_requested": all_projects_requested,
                "response_language": response_language,
            },
        )

        with tracer.start_as_current_span("assistant.agent.loop") as loop_span:
            loop_span.set_attribute("assistant.tool_count", len(tools))
            loop_span.set_attribute("assistant.required_tool_count", len(required_tools))
            answer, loop_result, _ = run_surface_loop(
                conn,
                request,
                messages=messages,
                context_frame=frame,
                tools=tools,
                intent="workspace",
                max_rounds=6,
            )
            loop_span.set_attribute(
                "assistant.loop_round_count", len(loop_result.tool_history)
            )
            loop_span.set_attribute(
                "assistant.loop_tool_call_count",
                sum(len(items) for items in loop_result.tool_history),
            )
        answer = str(answer or "").strip()
        if not answer:
            raise RuntimeError("The Workspace agent returned an empty response")

        with trace_context_scope(trace_context):
            if all_projects_requested:
                _complete_all_project_query_pages(
                    conn,
                    source_sink=source_sink,
                    tool_result_sink=tool_result_sink,
                    scope_mode=scope_mode,
                    project_ids=project_ids,
                    as_of=current,
                )

            grounded = ground_workspace_answer(
                answer,
                source_sink,
                tool_result_sink,
                progress_cb=progress_cb,
            )
            grounded_text = _strip_redundant_transition_markdown_tables(
                grounded.text,
                tool_result_sink,
            )
            finalized = finalize_grounded_reply(
                grounded_text,
                loop_result=loop_result,
                conn=conn,
                effective_project_id=(project_ids[0] if len(project_ids) == 1 else None),
                surface="web",
                grounding_errors=tuple(grounded.validation_errors),
            )
            answer = finalized.text
            validation_errors = list(finalized.grounding_errors)
            claim_violation_codes = list(finalized.claim_violation_codes)

            try:
                observe_exact_read_shadow(
                    conn,
                    trace_context=trace_context,
                    plan=exact_read_shadow,
                    served_answer=answer,
                    scope_mode=scope_mode,
                    project_ids=project_ids,
                    web_enabled=web_enabled,
                    routing_mode=routing_mode,
                    as_of=current,
                )
            except Exception:
                pass

            parts: list[dict[str, Any]] = [{"type": "text", "text": answer}]
            parts.extend(_tool_parts(tool_result_sink))
            parts.extend(source_sink)
            if validation_errors or claim_violation_codes:
                parts.append({
                    "type": "error",
                    "code": (
                        "partial_grounding"
                        if validation_errors
                        else "claim_validation"
                    ),
                    "message": (
                        "Some statements were replaced or omitted because their sources "
                        "or execution receipts could not be verified."
                    ),
                })
            tool_history = getattr(loop_result, "tool_history", ()) or ()
            ordered_tools = tuple(
                name for round_names in tool_history for name in round_names
            )
            exit_reason = str(
                getattr(loop_result, "exit_reason", "model_stop") or "model_stop"
            )
            round_count = int(
                getattr(loop_result, "round_count", len(tool_history)) or 0
            )
            _safe_workspace_run_finish(
                trace_context,
                status="completed",
                exit_reason=exit_reason,
                final_output=answer,
                persistence_status="not_requested",
                anomaly_flags=[
                    *(["partial_grounding"] if validation_errors else []),
                    *(["claim_violation"] if claim_violation_codes else []),
                ],
                metadata={
                    "round_count": round_count,
                    "tool_count": len(ordered_tools),
                    "grounding_error_count": len(validation_errors),
                    "claim_violation_count": len(claim_violation_codes),
                    "claim_violation_codes": claim_violation_codes,
                    "project_count": len(project_ids),
                },
            )
            _submit_tool_centric_shadow_fail_open(
                conn,
                question=message,
                served_answer=answer,
                scope_mode=scope_mode,
                project_ids=project_ids,
                as_of=current,
                routing_mode=routing_mode,
                trace_context=trace_context,
            )
            return WorkspaceAgentResult(
                text=answer,
                parts=parts,
                tool_names=ordered_tools,
                validation_errors=tuple(validation_errors),
                tool_results=tuple(tool_result_sink),
                trace_id=trace_context.trace_id,
                run_id=trace_context.run_id,
                turn_id=trace_context.turn_id,
            )
    except Exception as exc:
        with trace_context_scope(trace_context):
            _safe_workspace_run_finish(
                trace_context,
                status="error",
                exit_reason=type(exc).__name__,
                persistence_status="error",
                anomaly_flags=["error"],
                metadata={"error_class": type(exc).__name__},
            )
        raise


def _safe_workspace_run_start(context: TraceContext, **kwargs: Any) -> None:
    try:
        emit_run_start(context, **kwargs)
    except Exception:
        pass


def _safe_workspace_run_finish(context: TraceContext, **kwargs: Any) -> None:
    try:
        emit_run_finish(context, **kwargs)
    except Exception:
        pass

def _submit_tool_centric_shadow_fail_open(
    conn: sqlite3.Connection,
    *,
    question: str,
    served_answer: str,
    scope_mode: str,
    project_ids: list[str],
    as_of: datetime,
    routing_mode: str,
    trace_context: TraceContext,
) -> bool:
    """Submit an immutable shadow job without changing request behavior."""
    try:

        from src.assistant.tool_centric_serving import should_run_tool_centric_shadow

        if routing_mode != "production" or not should_run_tool_centric_shadow("workspace"):
            return False
        from src.assistant.tool_centric_shadow_worker import (
            ToolCentricShadowJob,
            database_path_for_connection,
            submit_tool_centric_shadow,
        )

        database_path = database_path_for_connection(conn)
        if not database_path:
            return False
        return submit_tool_centric_shadow(
            ToolCentricShadowJob(
                database_path=database_path,
                question=question,
                served_answer=served_answer,
                scope_mode=scope_mode,
                project_ids=tuple(project_ids),
                as_of=as_of,
                parent_trace_id=trace_context.trace_id,
                parent_run_id=trace_context.run_id,
                thread_id=trace_context.thread_id,
                turn_id=trace_context.turn_id,
            )
        )
    except Exception:
        # Research telemetry must never alter the served answer or persistence.
        return False


def _complete_all_project_query_pages(
    conn: sqlite3.Connection,
    *,
    source_sink: list[dict[str, Any]],
    tool_result_sink: list[dict[str, Any]],
    scope_mode: str,
    project_ids: list[str],
    as_of: datetime,
    max_pages: int = 100,
) -> None:
    """Fetch every remaining query_portfolio page for trusted all-project requests."""
    from src.tools.assistant_workspace_handlers import workspace_query_portfolio

    initial_traces = [
        item for item in list(tool_result_sink)
        if item.get("tool") == "query_portfolio"
        and isinstance(item.get("result"), dict)
        and not item["result"].get("error")
    ]
    for initial in initial_traces:
        cursor = str(initial["result"].get("next_cursor") or "").strip() or None
        seen_cursors: set[str] = set()
        page_number = 1
        while cursor and page_number < max_pages:
            if cursor in seen_cursors:
                initial["result"]["pagination_error"] = "query_portfolio returned a repeated cursor"
                break
            seen_cursors.add(cursor)
            public_arguments = dict(initial.get("arguments") or {})
            public_arguments.update({"cursor": cursor, "limit": 100})
            trusted_arguments = {
                **public_arguments,
                "_assistant_scope_mode": scope_mode,
                "_assistant_scope_project_ids": list(project_ids),
                "_assistant_source_sink": source_sink,
                "_assistant_as_of": as_of.isoformat(),
                "_assistant_all_projects_requested": True,
            }
            page_result = workspace_query_portfolio(conn, trusted_arguments)
            tool_result_sink.append({
                "tool": "query_portfolio",
                "arguments": public_arguments,
                "result": page_result,
            })
            if not isinstance(page_result, dict) or page_result.get("error"):
                break
            cursor = str(page_result.get("next_cursor") or "").strip() or None
            page_number += 1
        if cursor and page_number >= max_pages:
            initial["result"]["pagination_error"] = (
                f"query_portfolio pagination exceeded the {max_pages}-page safety limit"
            )


def _required_tools_for_request(
    message: str,
    *,
    project_ids: list[str],
    scope_mode: str,
) -> frozenset[str]:
    """Require the primary domain tool while leaving arguments and follow-up tools to the agent."""
    text = " ".join(message.lower().split())
    if re.search(r"\b(vendor|organisation|organization|counterparty|regulator|supplier)\b", text):
        return frozenset({"search_project_parties"})
    if re.search(
        r"\b(attention|aging|ageing|stale|overdue|priority|prioriti[sz]e|at risk|this week)\b|"
        r"needs? focus|what should (?:i|we) focus",
        text,
    ):
        return frozenset({"portfolio_attention"})
    if re.search(
        r"\b(unanswered|unreplied|awaiting (?:a )?reply|needs? (?:a )?reply|no (?:newer )?response|"
        r"inbound-last|without (?:a )?response)\b",
        text,
    ):
        return frozenset({"list_unanswered_threads"})
    if re.search(
        r"\b(evidence audit|audit (?:the )?evidence|source of truth|accepted update|rejected update|"
        r"superseded update|evidence conflict|conflicting evidence|why (?:is|was))\b",
        text,
    ):
        return frozenset({"get_project_evidence_audit"})
    if re.search(
        r"\b(milestone readiness|readiness|ready for|ready to (?:move|advance|progress|go live)|"
        r"can (?:it|we|the project) (?:move|advance|progress|go live)|exit criteria|acceptance criteria)\b",
        text,
    ):
        return frozenset({"assess_milestone_readiness"})
    if re.search(
        r"\b(timeline|chronolog|history|sequence of events|what happened|when did|emails? and activit(?:y|ies)|"
        r"recent emails?|project events?)\b",
        text,
    ):
        return frozenset({"search_project_timeline"})
    if re.search(
        r"\b(remaining work|outstanding work|project work|commitments?|follow-ups?|followups?|"
        r"open actions?|due items?|tasks? due|work items?)\b",
        text,
    ):
        return frozenset({"list_project_work"})
    asks_for_brief = re.search(
        r"\b(status|recent changes?|latest changes?|owner|ball(?: with)?|next action|what changed|summary|brief)\b",
        text,
    )
    mentioned_project = re.search(r"\b[A-Z]{2,}[A-Z0-9-]*20\d{2}\b", message)
    if (mentioned_project and asks_for_brief) or (len(project_ids) == 1 and asks_for_brief):
        return frozenset({"get_project_brief"})
    if text.startswith(("/compare", "/status")):
        if len(project_ids) == 1 and scope_mode == "projects":
            return frozenset({"get_project_brief"})
        return frozenset({"query_portfolio"})
    if re.search(r"\b(show|list|find|which projects?|all projects?|portfolio|compare|check all)\b", text):
        return frozenset({"query_portfolio"})
    return frozenset()


_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def _result_has_status_transition(result: dict[str, Any]) -> bool:
    filters = result.get("filters") if isinstance(result.get("filters"), dict) else {}
    if isinstance(filters.get("status_transition"), dict):
        return True
    effective_filters = (
        result.get("effective_filters")
        if isinstance(result.get("effective_filters"), dict)
        else {}
    )
    return isinstance(effective_filters.get("transition"), dict)


def _status_transition_targets(result: dict[str, Any]) -> set[str]:
    filters = result.get("filters") if isinstance(result.get("filters"), dict) else {}
    status_transition = filters.get("status_transition")
    if isinstance(status_transition, dict) and status_transition.get("to_status"):
        return {str(status_transition["to_status"]).strip().casefold()}

    effective_filters = (
        result.get("effective_filters")
        if isinstance(result.get("effective_filters"), dict)
        else {}
    )
    transition = effective_filters.get("transition")
    if not isinstance(transition, dict):
        return set()
    return {
        str(value).strip().casefold()
        for value in transition.get("to_values") or []
        if str(value).strip()
    }


def _strip_redundant_transition_markdown_tables(
    answer: str,
    tool_results: list[dict[str, Any]],
) -> str:
    """Remove transition tables already rendered as structured UI parts."""
    has_transition_table = any(
        str(item.get("tool") or "") in {"query_portfolio", "portfolio_query_v2"}
        and isinstance(item.get("result"), dict)
        and _result_has_status_transition(item["result"])
        and isinstance(item["result"].get("results"), list)
        and bool(item["result"]["results"])
        for item in tool_results
    )
    if not has_transition_table:
        return answer

    lines = answer.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines) and _MARKDOWN_TABLE_SEPARATOR_RE.fullmatch(lines[index + 1].strip()):
            header = lines[index].strip().casefold()
            if (
                header.startswith("|")
                and "project" in header
                and "client" in header
                and any(
                    term in header
                    for term in (
                        "completed",
                        "transition",
                        "status changed",
                        "\u8f49\u70ba",
                        "\u5b8c\u6210",
                    )
                )
            ):
                index += 2
                while index < len(lines):
                    row = lines[index].strip()
                    if not (row.startswith("|") and row.endswith("|") and row.count("|") >= 3):
                        break
                    index += 1
                while kept and not kept[-1].strip():
                    kept.pop()
                continue
        kept.append(lines[index])
        index += 1

    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _tool_parts(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build one rich result group per tool, merging deterministic pagination pages."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in tool_results:
        tool = str(item.get("tool") or "")
        if not tool:
            continue
        if tool not in grouped:
            grouped[tool] = []
            order.append(tool)
        grouped[tool].append(item)

    parts: list[dict[str, Any]] = []
    for tool in order:
        traces = grouped[tool]
        results = [
            item.get("result") if isinstance(item.get("result"), dict) else {}
            for item in traces
        ]
        errors = [str(result.get("error")) for result in results if result.get("error")]
        rows = _merge_result_rows(results)
        text = errors[0] if errors else None
        if not errors and len(traces) > 1:
            text = f"Fetched {len(traces)} pages and {len(rows)} records"
        parts.append({
            "type": "tool_result",
            "tool": tool,
            "status": "failed" if errors else "completed",
            "text": text,
            "filters": next((result.get("filters") for result in results if result.get("filters")), None),
        })
        if errors:
            continue
        parts.extend(_rich_parts_for_tool(tool, results, rows))
    return parts


def _merge_result_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        page = result.get("results") if isinstance(result.get("results"), list) else []
        for row in page:
            if not isinstance(row, dict):
                continue
            identity = str(
                row.get("source_id")
                or row.get("project_id")
                or row.get("record_id")
                or row.get("work_id")
                or row.get("latest_email_id")
                or repr(sorted(row.items()))
            )
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(dict(row))
    return rows


def _table_part(
    *,
    title: str,
    columns: list[tuple[str, str]],
    rows: list[dict[str, Any]],
    export_filename: str,
) -> dict[str, Any]:
    return {
        "type": "table",
        "title": title,
        "columns": [{"key": key, "label": label} for key, label in columns],
        "rows": rows,
        "export_filename": export_filename,
    }


def _rich_parts_for_tool(
    tool: str,
    results: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if tool == "portfolio_attention" and rows:
        normalized = [
            {**row, "attention_reasons": ", ".join(row.get("attention_reasons") or [])}
            for row in rows
        ]
        return [_table_part(
            title="Projects needing attention",
            columns=[
                ("project_id", "Project"), ("client_name", "Client"),
                ("project_status", "Status"), ("current_milestone", "Milestone"),
                ("ball_with", "Ball with"), ("priority_score", "Priority"),
                ("days_inactive", "Days inactive"), ("attention_reasons", "Attention reasons"),
                ("next_action", "Next action"),
            ],
            rows=normalized,
            export_filename="ai-workspace-attention.csv",
        )]
    if tool == "search_project_parties" and rows:
        return [_table_part(
            title="Project parties",
            columns=[
                ("project_id", "Project"), ("client_name", "Client"),
                ("org_name", "Organisation"), ("party_role", "Role"),
                ("project_status", "Status"), ("current_milestone", "Milestone"),
            ],
            rows=rows,
            export_filename="ai-workspace-project-parties.csv",
        )]
    if tool == "portfolio_query_v2" and rows:
        field_order = next(
            (
                [str(item) for item in result.get("fields") or []]
                for result in results
                if isinstance(result.get("fields"), list)
            ),
            [],
        )
        labels = {
            "project_id": "Project",
            "client_name": "Client",
            "project_status": "Status",
            "current_milestone": "Milestone",
            "ball_with": "Ball with",
            "market": "Market",
            "target_go_live": "Target go-live",
            "updated_at": "Updated",
            "transitioned_at": "Status changed",
        }
        columns = [
            (field, labels.get(field, field.replace("_", " ").title()))
            for field in field_order
        ]
        return [_table_part(
            title="Portfolio query",
            columns=columns,
            rows=rows,
            export_filename="ai-workspace-portfolio-query-v2.csv",
        )]
    if tool == "query_portfolio" and rows:
        if any(_result_has_status_transition(result) for result in results):
            targets = {
                target
                for result in results
                for target in _status_transition_targets(result)
            }
            completed_transition = targets == {"completed"}
            return [_table_part(
                title="Completed projects" if completed_transition else "Project status transitions",
                columns=[
                    ("project_id", "Project"),
                    ("client_name", "Client"),
                    (
                        "transitioned_at",
                        "Completed on" if completed_transition else "Status changed",
                    ),
                ],
                rows=rows,
                export_filename="ai-workspace-status-transitions.csv",
            )]
        return [_table_part(
            title="Portfolio query",
            columns=[
                ("project_id", "Project"), ("client_name", "Client"), ("market", "Market"),
                ("project_status", "Status"), ("current_milestone", "Milestone"),
                ("ball_with", "Ball with"), ("next_action", "Next action"),
            ],
            rows=rows,
            export_filename="ai-workspace-query.csv",
        )]
    if tool == "search_project_timeline" and rows:
        return [_table_part(
            title="Project timeline",
            columns=[
                ("occurred_at", "Occurred"), ("kind", "Type"), ("title", "Title"),
                ("summary", "Summary"), ("direction", "Direction"), ("thread_key", "Thread"),
            ],
            rows=rows,
            export_filename="ai-workspace-project-timeline.csv",
        )]
    if tool == "list_project_work" and rows:
        return [_table_part(
            title="Project work",
            columns=[
                ("work_type", "Type"), ("title", "Work item"), ("status", "Status"),
                ("owner", "Owner"), ("due_at", "Due"),
                ("provenance_quality", "Evidence quality"), ("source_ref", "Source"),
            ],
            rows=rows,
            export_filename="ai-workspace-project-work.csv",
        )]
    if tool == "list_unanswered_threads" and rows:
        return [_table_part(
            title="Unanswered thread candidates",
            columns=[
                ("project_id", "Project"), ("client_name", "Client"), ("subject", "Subject"),
                ("sender_email", "Sender"), ("occurred_at", "Latest inbound"),
                ("age_hours", "Age (hours)"), ("message_count", "Messages"),
                ("candidate_state", "Candidate state"),
            ],
            rows=rows,
            export_filename="ai-workspace-unanswered-threads.csv",
        )]
    if tool == "get_project_evidence_audit" and results:
        result = results[-1]
        output: list[dict[str, Any]] = []
        current = result.get("current_truth") if isinstance(result.get("current_truth"), dict) else None
        if current:
            output.append(_table_part(
                title="Authoritative project truth",
                columns=[
                    ("project_id", "Project"), ("client_name", "Client"),
                    ("project_status", "Status"), ("current_milestone", "Milestone"),
                    ("ball_with", "Ball with"), ("next_action", "Next action"),
                    ("updated_at", "Updated"),
                ],
                rows=[current],
                export_filename="ai-workspace-current-truth.csv",
            ))
        conflicts = result.get("conflicts") if isinstance(result.get("conflicts"), list) else []
        if conflicts:
            output.append(_table_part(
                title="Evidence conflicts",
                columns=[
                    ("type", "Conflict"), ("field", "Field"), ("current_value", "Current truth"),
                    ("proposed_value", "Proposed value"), ("history_value", "History value"),
                    ("update_id", "Update"),
                ],
                rows=[item for item in conflicts if isinstance(item, dict)],
                export_filename="ai-workspace-evidence-conflicts.csv",
            ))
        proposals = result.get("proposed_updates") if isinstance(result.get("proposed_updates"), list) else []
        if proposals:
            output.append(_table_part(
                title="Proposed update decisions",
                columns=[
                    ("update_id", "Update"), ("decision_outcome", "Decision"),
                    ("proposed_changes", "Proposed changes"), ("source_ref", "Source"),
                    ("created_at", "Created"), ("is_superseded", "Superseded"),
                ],
                rows=[item for item in proposals if isinstance(item, dict)],
                export_filename="ai-workspace-proposed-update-audit.csv",
            ))
        return output
    if tool == "assess_milestone_readiness" and results:
        result = results[-1]
        summary = {
            "project_id": result.get("project_id"),
            "assessment_status": result.get("assessment_status"),
            "current_milestone": result.get("current_milestone"),
            "target_milestone": result.get("target_milestone"),
            "decision": result.get("decision"),
            "policy_version": result.get("policy_version"),
            "reason": result.get("reason"),
        }
        output = [_table_part(
            title="Milestone readiness",
            columns=[
                ("project_id", "Project"), ("assessment_status", "Readiness"),
                ("current_milestone", "Current milestone"), ("target_milestone", "Target milestone"),
                ("decision", "Decision"), ("policy_version", "Policy"), ("reason", "Reason"),
            ],
            rows=[summary],
            export_filename="ai-workspace-milestone-readiness.csv",
        )]
        criteria = result.get("criteria") if isinstance(result.get("criteria"), list) else []
        if not criteria:
            criteria = result.get("required_exit_criteria") if isinstance(result.get("required_exit_criteria"), list) else []
        if criteria:
            output.append(_table_part(
                title="Readiness criteria",
                columns=[
                    ("criterion_id", "Criterion"), ("description", "Description"),
                    ("result", "Result"), ("reason", "Reason"), ("citations", "Evidence"),
                ],
                rows=[item for item in criteria if isinstance(item, dict)],
                export_filename="ai-workspace-readiness-criteria.csv",
            ))
        return output
    return []
