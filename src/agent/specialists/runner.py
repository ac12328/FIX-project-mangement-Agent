from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager, nullcontext
from typing import Any, Iterable, Iterator

from src.agent.tool_receipts import ToolExecutionReceipt
from src.agent.core_loop import run_agent_loop
from src.agent.control_plane_metrics import control_plane_metrics
from src.agent.loop_config import LoopConfig
from src.agent.skills.registry import get_skill
from src.agent.specialists.context import (
    build_specialist_context,
    load_skill_system_prompt,
)
from src.agent.specialists.contracts import (
    SpecialistEvidence,
    SpecialistFailureEvent,
    SpecialistReport,
    SpecialistType,
)
from src.agent.specialists.sessions import (
    ResumeError,
    build_resume_messages,
    load_for_resume,
    store_session,
)
from src.agent.specialists.tools import get_tools_for_specialist
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

logger = logging.getLogger(__name__)


def _infer_explicit_operator_instruction(task: str, specialist_type: SpecialistType) -> bool:
    """Recognize explicit authorization from the trusted delegate request boundary."""
    if specialist_type != SpecialistType.WRITE:
        return False
    text = (task or "").casefold()
    authorization_terms = (
        "operator explicitly", "explicitly authorized", "explicitly authorised",
        "operator said yes", "operator confirmed", "operator approved",
    )
    action_terms = ("update ", "apply ", "set ", "change ", "confirm in this run")
    return any(term in text for term in authorization_terms) and any(term in text for term in action_terms)

_DEFAULT_MAX_ROUNDS = {
    SpecialistType.STATUS: 3,
    SpecialistType.RESEARCH: 6,
    SpecialistType.WRITE: 6,
    SpecialistType.DRAFT: 4,
    SpecialistType.PORTFOLIO: 3,
    SpecialistType.SUPPORT: 4,
    SpecialistType.EMAIL: 5,
}


def _flatten_receipts(
    receipts: Iterable[ToolExecutionReceipt],
) -> list[ToolExecutionReceipt]:
    """Return top-level and delegated receipts in deterministic depth-first order."""
    flattened: list[ToolExecutionReceipt] = []
    pending = list(reversed(list(receipts)))
    while pending:
        receipt = pending.pop()
        flattened.append(receipt)
        pending.extend(reversed(receipt.child_receipts))
    return flattened


def _attach_receipt_evidence(report: SpecialistReport) -> None:
    """Promote successful bounded receipt evidence into the specialist contract."""
    evidence: list[SpecialistEvidence] = []
    seen: set[tuple[str, str, str | None]] = set()
    sources = list(report.sources)
    for receipt in _flatten_receipts(report.execution_receipts):
        if receipt.status != "succeeded":
            continue
        for item in receipt.evidence:
            key = (item.source_type, item.locator, item.content_hash)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(SpecialistEvidence(
                source_type=item.source_type,
                locator=item.locator,
                authority=item.authority,
                status=item.status,
                content_hash=item.content_hash,
                excerpt=item.excerpt,
                claim_coverage=list(item.claim_coverage),
            ))
            if item.locator and item.locator not in sources:
                sources.append(item.locator)
    report.evidence = evidence
    report.sources = sources


def _has_usable_evidence(report: SpecialistReport) -> bool:
    return any(item.excerpt or item.content_hash for item in report.evidence)


def run_specialist_loop(
    conn: sqlite3.Connection,
    specialist_type: SpecialistType,
    task: str,
    project_id: str | None = None,
    *,
    skill_id: str | None = None,
    resume_from: str | None = None,
    operator_id: str | None = None,
    thread_id: str | None = None,
    chat_id: int | None = None,
    explicit_operator_instruction: bool = False,
    deterministic_authorization: bool = False,
    operator_request_text: str | None = None,
    read_only_execution: bool = False,
    dashboard_permissions: Iterable[str] = (),
    trace_context: TraceContext | None = None,
    parent_tool_call_id: str | None = None,
    deadline_monotonic: float | None = None,
    shared_execution_context: dict[str, Any] | None = None,
) -> SpecialistReport:
    """Run a specialist as a standalone run or a correlated delegated step."""
    skill = get_skill(skill_id or "")
    resolved_skill_id = skill.skill_id if skill else (skill_id or specialist_type.value)
    active_context = current_trace_context()
    context = trace_context or active_context
    owns_context = context is None
    if context is None:
        context = TraceContext.create(
            surface="tool",
            task=f"specialist:{resolved_skill_id}",
            thread_id=thread_id or (str(chat_id) if chat_id is not None else "default"),
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
                    "surface": "tool",
                    "task": f"specialist:{resolved_skill_id}",
                    "specialist_type": specialist_type.value,
                    "skill_id": resolved_skill_id,
                    "project_id": project_id,
                    "thread_id": thread_id,
                    "chat_id": chat_id,
                    "resume_from": resume_from,
                    "entrypoint": "run_specialist_loop",
                },
                payload={"user_input": task},
            )
        emit_event(
            context,
            "specialist.start",
            tool_call_id=parent_tool_call_id,
            status="running",
            metadata={
                "specialist_type": specialist_type.value,
                "skill_id": resolved_skill_id,
                "project_id": project_id,
                "resume_from": resume_from,
                "standalone": owns_context,
            },
            payload={"task": task},
        )
        try:
            report = _run_specialist_loop_body(
                conn,
                specialist_type,
                task,
                project_id=project_id,
                skill_id=skill_id,
                resume_from=resume_from,
                operator_id=operator_id,
                thread_id=thread_id,
                chat_id=chat_id,
                explicit_operator_instruction=explicit_operator_instruction,
                deterministic_authorization=deterministic_authorization,
                operator_request_text=operator_request_text,
                read_only_execution=read_only_execution,
                dashboard_permissions=dashboard_permissions,
                trace_context=context,
                deadline_monotonic=deadline_monotonic,
                shared_execution_context=shared_execution_context,
            )
            lifecycle_status = "completed" if report.ok else "error"
            persistence_status = "completed" if report.session_id else "not_observed"
            exit_reason = (
                report.exit_reason
                or report.error
                or report.completion_status
                or ("completed" if report.ok else "failed")
            )
            emit_event(
                context,
                "specialist.end",
                tool_call_id=parent_tool_call_id,
                status=lifecycle_status,
                metadata={
                    "specialist_type": specialist_type.value,
                    "skill_id": resolved_skill_id,
                    "project_id": project_id,
                    "session_id": report.session_id,
                    "completion_status": report.completion_status,
                    "exit_reason": exit_reason,
                    "persistence_status": persistence_status,
                    "committed": report.committed,
                    "outcome": report.outcome,
                },
                payload={"report": report.model_dump(mode="json")},
            )
            if owns_context:
                emit_run_finish(
                    context,
                    status=lifecycle_status,
                    exit_reason=str(exit_reason),
                    final_output=report.model_dump(mode="json"),
                    persistence_status=persistence_status,
                    anomaly_flags=[] if report.ok else ["specialist_failed"],
                    metadata={
                        "specialist_type": specialist_type.value,
                        "skill_id": resolved_skill_id,
                        "project_id": project_id,
                        "session_id": report.session_id,
                        "completion_status": report.completion_status,
                    },
                )
            return report
        except Exception as exc:
            emit_event(
                context,
                "specialist.error",
                tool_call_id=parent_tool_call_id,
                status="error",
                metadata={
                    "specialist_type": specialist_type.value,
                    "skill_id": resolved_skill_id,
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
                        "specialist_type": specialist_type.value,
                        "skill_id": resolved_skill_id,
                        "project_id": project_id,
                        "error_class": type(exc).__name__,
                    },
                )
            raise


def _run_specialist_loop_body(
    conn: sqlite3.Connection,
    specialist_type: SpecialistType,
    task: str,
    project_id: str | None = None,
    *,
    skill_id: str | None = None,
    resume_from: str | None = None,
    operator_id: str | None = None,
    thread_id: str | None = None,
    chat_id: int | None = None,
    explicit_operator_instruction: bool = False,
    deterministic_authorization: bool = False,
    operator_request_text: str | None = None,
    read_only_execution: bool = False,
    dashboard_permissions: Iterable[str] = (),
    trace_context: TraceContext | None = None,
    deadline_monotonic: float | None = None,
    shared_execution_context: dict[str, Any] | None = None,
) -> SpecialistReport:
    """Thin wrapper: build LoopConfig → run_agent_loop → SpecialistReport.

    When ``resume_from`` is set, continues a prior specialist transcript
    (same skill always; cross-skill only via allowlist). Tools always
    re-render for the *target* skill — not a union with the prior skill.
    """
    skill = get_skill(skill_id or "")
    resolved_skill_id = skill.skill_id if skill else (skill_id or specialist_type.value)
    explicit_operator_instruction = bool(
        explicit_operator_instruction
        or _infer_explicit_operator_instruction(task, specialist_type)
    )
    parent_session_id: str | None = None

    from src.tools.executor import get_active_chat_id
    from config import defaults

    active_chat_id = chat_id if chat_id is not None else get_active_chat_id()
    operator_id = operator_id or getattr(defaults, "OPERATOR_ID", None) or "default_operator"
    thread_id = thread_id or (str(active_chat_id) if active_chat_id is not None else "default")

    if resume_from:
        try:
            prior = load_for_resume(
                conn,
                resume_from,
                to_skill_id=resolved_skill_id,
                operator_id=operator_id,
                thread_id=thread_id,
                project_id=project_id,
            )
        except ResumeError as exc:
            control_plane_metrics.increment("specialist_resume", outcome="rejected")
            control_plane_metrics.increment("specialist_outcome", outcome="failed")
            return SpecialistReport(
                specialist_type=specialist_type,
                ok=False,
                summary=f"Resume rejected: {exc.message}",
                error=exc.code,
                skill_id=resolved_skill_id,
            )
        except sqlite3.OperationalError:
            # query_only conn — retry via scoped_write/read path
            try:
                with _writable_conn(conn) as wconn:
                    prior = load_for_resume(
                        wconn,
                        resume_from,
                        to_skill_id=resolved_skill_id,
                        operator_id=operator_id,
                        thread_id=thread_id,
                        project_id=project_id,
                    )
            except ResumeError as rexc:
                control_plane_metrics.increment(
                    "specialist_resume", outcome="rejected"
                )
                control_plane_metrics.increment(
                    "specialist_outcome", outcome="failed"
                )
                return SpecialistReport(
                    specialist_type=specialist_type,
                    ok=False,
                    summary=f"Resume rejected: {rexc.message}",
                    error=rexc.code,
                    skill_id=resolved_skill_id,
                )
            except Exception as load_exc:
                logger.exception("Failed to load resume session")
                control_plane_metrics.increment(
                    "specialist_resume", outcome="rejected"
                )
                control_plane_metrics.increment(
                    "specialist_outcome", outcome="failed"
                )
                return SpecialistReport(
                    specialist_type=specialist_type,
                    ok=False,
                    summary=f"Resume load failed: {load_exc}",
                    error="resume_load_failed",
                    skill_id=resolved_skill_id,
                )

        parent_session_id = prior.session_id
        control_plane_metrics.increment("specialist_resume", outcome="accepted")
        # Prefer prior project if caller omitted
        if not project_id and prior.project_id:
            project_id = prior.project_id
        skill_system = load_skill_system_prompt(resolved_skill_id, specialist_type)
        messages = build_resume_messages(
            prior,
            new_task=task,
            skill_system_prompt=skill_system,
            project_id=project_id,
        )
        frame = ContextFrame(identity=skill_system)
    else:
        frame, messages = build_specialist_context(
            conn,
            specialist_type,
            task,
            project_id=project_id,
            skill_id=skill.skill_id if skill else skill_id,
            operator_id=operator_id,
        )

    # Target skill tools only (never union with prior skill tools)
    tools = get_tools_for_specialist(
        specialist_type, skill_id=skill.skill_id if skill else skill_id
    )
    permissions = tuple(sorted({str(item) for item in dashboard_permissions}))
    if read_only_execution:
        from src.tools.registry import TOOL_BY_NAME

        tools = [
            tool
            for tool in tools
            if (
                (contract := TOOL_BY_NAME.get(tool.get("function", {}).get("name", "")))
                is not None
                and contract.side_effect_type in {"none", "read"}
                and contract.name != "delegate"
            )
        ]
    max_rounds = skill.max_rounds if skill else _DEFAULT_MAX_ROUNDS.get(specialist_type, 4)
    specialist_execution_context: dict[str, Any] = {
        key: value
        for key, value in (shared_execution_context or {}).items()
        if key in {
            "assistant_source_sink",
            "assistant_tool_result_sink",
            "assistant_as_of",
            "assistant_scope_mode",
            "assistant_scope_project_ids",
            "inbound_analysis_policy",
        }
    }
    specialist_execution_context.update({
        "read_only_execution": read_only_execution,
        "dashboard_permissions": permissions,
        "actor_user_id": operator_id,
        "specialist_type": specialist_type.value,
    })
    if deadline_monotonic is not None:
        specialist_execution_context["deadline_monotonic"] = deadline_monotonic
    if operator_request_text:
        specialist_execution_context["operator_request_text"] = operator_request_text

    if trace_context is not None:
        emit_event(
            trace_context,
            "context.composed",
            metadata={
                **context_composition_metadata(
                    messages,
                    profile=f"specialist:{resolved_skill_id}",
                    context_frame=frame,
                ),
                **tool_contract_metadata(tools),
                "surface": "tool",
                "task": f"specialist:{resolved_skill_id}",
                "specialist_type": specialist_type.value,
                "skill_id": resolved_skill_id,
                "project_id": project_id,
                "tool_names": [
                    str(tool.get("function", {}).get("name") or "")
                    for tool in tools
                    if tool.get("function", {}).get("name")
                ],
                "resume_from": resume_from,
            },
            payload={"ordered_messages": messages},
        )

    prior_tool_history = list(prior.tool_history) if resume_from else []
    initial_checkpoint = {
        "schema_version": 1,
        "phase": "initialized",
        "round_index": 0,
        "exit_reason": None,
        "messages": messages,
        "tool_history": prior_tool_history,
        "execution_receipts": [],
        "guard_results": [],
    }
    session_id = _persist_session(
        conn,
        messages=messages,
        skill_id=resolved_skill_id,
        specialist_type=specialist_type.value,
        operator_id=operator_id,
        thread_id=thread_id,
        project_id=project_id,
        parent_session_id=parent_session_id,
        tool_history=prior_tool_history,
        checkpoint=initial_checkpoint,
        status="in_progress",
    )
    if session_id:
        control_plane_metrics.increment(
            "specialist_checkpoint", phase="initialized"
        )
    latest_checkpoint = initial_checkpoint

    def _checkpoint(payload: dict[str, Any]) -> None:
        nonlocal latest_checkpoint
        latest_checkpoint = dict(payload)
        control_plane_metrics.increment(
            "specialist_checkpoint", phase=payload.get("phase")
        )
        if not session_id:
            return
        current_messages = payload.get("messages")
        current_history = payload.get("tool_history")
        _persist_session(
            conn,
            session_id=session_id,
            messages=current_messages if isinstance(current_messages, list) else messages,
            skill_id=resolved_skill_id,
            specialist_type=specialist_type.value,
            operator_id=operator_id,
            thread_id=thread_id,
            project_id=project_id,
            parent_session_id=parent_session_id,
            tool_history=(
                prior_tool_history + current_history
                if isinstance(current_history, list)
                else prior_tool_history
            ),
            checkpoint=payload,
            status="in_progress",
        )

    # IMPORTANT: do NOT pass the caller's conn into the specialist loop.
    # Host invokes delegate via side_effect_type="read", so the connection is
    # often query_only=ON. Write tools must self-scope via scoped_write/read.
    config = LoopConfig(
        caller_name=f"specialist:{resolved_skill_id}",
        surface="tool",
        tools=tools,
        intent=resolved_skill_id,
        messages=messages,
        context_frame=frame,
        exposure_scopes=(frame.memory_exposure_scope(
            operator_id=operator_id, surface="tool", task_type=resolved_skill_id,
            project_id=project_id,
        ),),
        max_rounds=max_rounds,
        reserve_final_response_round=True,
        effective_project_id=project_id,
        conn=None,
        chat_id=active_chat_id,
        enable_compression=True,
        refresh_anchors_each_round=True,
        enable_fabrication_guard=True,
        operator_id=operator_id,
        thread_id=thread_id,
        explicit_operator_instruction=explicit_operator_instruction,
        deterministic_authorization=deterministic_authorization,
        operator_request_text=operator_request_text,
        execution_context=specialist_execution_context,
        trace_context=trace_context,
        deadline_monotonic=deadline_monotonic,
        on_checkpoint=_checkpoint,
    )
    from src.tools.executor import trusted_execution_context
    try:
        with trusted_execution_context(
            operator_id=operator_id,
            chat_id=active_chat_id,
            explicit_operator_instruction=explicit_operator_instruction,
            deterministic_authorization=deterministic_authorization,
            operator_request_text=operator_request_text,
            read_only_execution=read_only_execution,
            dashboard_permissions=permissions,
            actor_user_id=operator_id,
            specialist_type=specialist_type.value,
            deadline_monotonic=deadline_monotonic,
        ):
            result = run_agent_loop(config)
    except Exception as exc:
        failed_checkpoint = dict(latest_checkpoint)
        failed_checkpoint.update({
            "phase": "failed",
            "exit_reason": type(exc).__name__,
            "failure": {"error_class": type(exc).__name__},
        })
        if session_id:
            _persist_session(
                conn,
                session_id=session_id,
                messages=(failed_checkpoint.get("messages") or messages),
                skill_id=resolved_skill_id,
                specialist_type=specialist_type.value,
                operator_id=operator_id,
                thread_id=thread_id,
                project_id=project_id,
                parent_session_id=parent_session_id,
                tool_history=(failed_checkpoint.get("tool_history") or prior_tool_history),
                checkpoint=failed_checkpoint,
                status="failed",
            )
            control_plane_metrics.increment(
                "specialist_checkpoint", phase="failed"
            )
        control_plane_metrics.increment("specialist_outcome", outcome="failed")
        raise
    report = _parse_report(specialist_type, result.reply)
    report.skill_id = resolved_skill_id
    report.exit_reason = result.exit_reason
    report.execution_receipts = list(result.execution_receipts or [])
    _attach_receipt_evidence(report)
    _apply_receipt_metadata(report, specialist_type)

    if result.exit_reason == "deadline_exceeded":
        report.failure_events = [SpecialistFailureEvent(
            code="execution_deadline_exceeded",
            phase="specialist_loop",
            message="The specialist deadline expired before normal finalization.",
            retryable=True,
        )]
        if _has_usable_evidence(report):
            report.ok = True
            report.completion_status = "partial"
            report.outcome = "partial"
            report.error = None
            if not report.summary or "deadline" in report.summary.casefold():
                report.summary = (
                    "Partial result: verified evidence was retained before the "
                    "specialist deadline expired."
                )
            status = "partial"
        else:
            report.ok = False
            report.completion_status = "timed_out"
            report.outcome = "timed_out"
            report.error = "execution_deadline_exceeded"
            status = "timed_out"
    elif result.exit_reason == "exhausted":
        report.ok = report.committed
        report.completion_status = "completed" if report.committed else "exhausted"
        report.outcome = report.outcome if report.committed else "exhausted"
        report.error = None if report.committed else (report.error or "specialist_exhausted")
        status = "completed" if report.committed else "exhausted"
    elif result.exit_reason in {"llm_error", "claim_violation"}:
        report.ok = report.committed
        report.completion_status = "completed" if report.committed else "failed"
        report.error = None if report.committed else (report.error or result.exit_reason)
        status = "completed" if report.committed else "failed"
    elif not report.ok:
        report.completion_status = "failed"
        status = "failed"
    elif specialist_type == SpecialistType.WRITE and not _has_write_completion_receipt(
        report.execution_receipts
    ):
        report.ok = False
        report.completion_status = "unverified"
        report.outcome = "unverified"
        report.error = report.error or "missing_write_receipt"
        status = "unverified"
    else:
        report.completion_status = "completed"
        status = "completed"
    final_checkpoint = dict(latest_checkpoint)
    final_checkpoint.update({
        "phase": "finalized",
        "exit_reason": result.exit_reason,
        "messages": result.messages or messages,
        "tool_history": list(result.tool_history or []),
        "execution_receipts": [
            receipt.to_dict() for receipt in report.execution_receipts
        ],
        "guard_results": list(result.guard_results or []),
        "report": {
            "completion_status": report.completion_status,
            "outcome": report.outcome,
            "ok": report.ok,
            "committed": report.committed,
            "evidence": [item.model_dump(mode="json") for item in report.evidence],
            "failure_events": [
                item.model_dump(mode="json") for item in report.failure_events
            ],
        },
    })
    persisted_session_id = _persist_session(
        conn,
        session_id=session_id,
        messages=result.messages or messages,
        skill_id=resolved_skill_id,
        specialist_type=specialist_type.value,
        operator_id=operator_id,
        thread_id=thread_id,
        project_id=project_id,
        parent_session_id=parent_session_id,
        tool_history=(
            prior_tool_history + list(result.tool_history or [])
        ),
        checkpoint=final_checkpoint,
        status=status,
    )
    control_plane_metrics.increment(
        "specialist_checkpoint", phase="finalized"
    )
    control_plane_metrics.increment("specialist_outcome", outcome=status)
    if persisted_session_id:
        report.session_id = persisted_session_id
        report.resume_token = persisted_session_id
    return report


def _persist_session(
    conn: sqlite3.Connection | None,
    **kwargs: Any,
) -> str | None:
    try:
        with _writable_conn(conn) as wconn:
            return store_session(wconn, **kwargs)
    except Exception:
        logger.exception("Failed to persist specialist session")
        return None


@contextmanager
def _writable_conn(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """Use caller's conn when writable; else scoped_write (production query_only)."""
    if conn is not None:
        try:
            row = conn.execute("PRAGMA query_only").fetchone()
            qonly = bool(row[0]) if row is not None else False
        except sqlite3.Error:
            qonly = False
        if not qonly:
            yield conn
            return
    from src.db.connection import scoped_write

    with scoped_write() as wconn:
        yield wconn


def _has_write_completion_receipt(
    receipts: Iterable[ToolExecutionReceipt],
) -> bool:
    """Require a successful write/external receipt, staged write, or verified no-op."""
    from src.tools.registry import TOOL_BY_NAME

    pending = list(receipts)
    while pending:
        receipt = pending.pop()
        pending.extend(receipt.child_receipts)
        if receipt.status != "succeeded":
            continue
        if receipt.committed or receipt.outcome in {"staged", "verified_noop"}:
            return True
        contract = TOOL_BY_NAME.get(receipt.tool_name)
        if contract is not None and contract.side_effect_type in {"write", "external"}:
            return True
    return False


def _apply_receipt_metadata(
    report: SpecialistReport,
    specialist_type: SpecialistType,
) -> None:
    """Derive trusted state without poisoning recovered read-only results."""
    receipts = _flatten_receipts(report.execution_receipts)
    committed = [
        receipt for receipt in receipts
        if receipt.status == "succeeded" and receipt.committed
    ]
    staged = [
        receipt for receipt in receipts
        if receipt.status == "succeeded" and receipt.outcome == "staged"
    ]
    failed = [
        receipt for receipt in receipts
        if receipt.status in {"failed", "denied"}
    ]

    if committed:
        receipt = committed[-1]
        report.committed = True
        report.outcome = receipt.outcome
        report.operation_id = receipt.operation_id
        report.ok = True
        report.error = None
        return
    if staged:
        receipt = staged[-1]
        report.committed = False
        report.outcome = "staged"
        report.operation_id = receipt.operation_id
        report.ok = True
        report.error = None
        return
    if failed:
        # Read-only specialists may recover from an earlier lookup failure and
        # still return a verified report from later successful tool results.
        if specialist_type != SpecialistType.WRITE and report.ok and (
            report.summary or report.findings or report.sources
        ):
            report.committed = False
            report.outcome = "completed"
            report.error = None
            return
        receipt = failed[-1]
        report.committed = False
        report.outcome = receipt.outcome
        report.operation_id = receipt.operation_id
        report.ok = False
        report.error = report.error or receipt.error_code or receipt.outcome


def _parse_report(specialist_type: SpecialistType, reply: str) -> SpecialistReport:
    data: dict[str, Any] | None = None
    candidates = [reply.strip()]
    fenced = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        reply,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        candidates.append(fenced.group(1))
    first_brace = reply.find("{")
    last_brace = reply.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(reply[first_brace:last_brace + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            data = parsed
            break

    if data is not None:
        # These fields are authoritative runtime metadata, not model output.
        for reserved in (
            "session_id", "resume_token", "completion_status", "exit_reason", "outcome",
            "committed", "operation_id", "execution_receipts", "evidence",
            "failure_events",
        ):
            data.pop(reserved, None)
        return SpecialistReport(specialist_type=specialist_type, **data)

    # Preserve legacy text reports for read/draft specialists. Project-write
    # specialists must return a structured report so prose alone can never
    # be interpreted as proof that a database write committed.
    if specialist_type != SpecialistType.WRITE:
        return SpecialistReport(
            specialist_type=specialist_type,
            ok=True,
            summary=reply.strip(),
        )
    return SpecialistReport(
        specialist_type=specialist_type,
        ok=False,
        completion_status="unverified",
        summary=reply.strip(),
        error="unstructured_specialist_report",
    )
