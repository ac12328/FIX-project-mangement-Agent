"""Chat tool handler functions — extracted from executor.py to keep files ≤ 500 lines."""

from __future__ import annotations

import copy
import json
import logging
import math
import sqlite3
import threading
from typing import Any

from src.db.savepoint import savepoint
from src.models.enums import Milestone
from src.repositories import contact_repo, project_repo
from src.repositories.working_memory_repo import delete_memory, get_memory, upsert_memory

# Thread-safe storage for email analysis results — written by report_email_analysis tool,
# read by scheduler_jobs after agent_loop returns. Uses a lock + LIFO rather than ContextVar
# because asyncio.to_thread copies context (ContextVar changes in thread are invisible to
# caller). The LIFO is keyed by project_id so a concurrent chat analyzing a DIFFERENT project
# cannot overwrite the scheduler's in-flight result for its own project.
_email_analysis_lock = threading.Lock()
_email_analysis_results: list[dict] = []
_email_analysis_idempotency_results: dict[str, dict] = {}
_EMAIL_ANALYSIS_MAX = 16
_EMAIL_PROPOSABLE_FIELDS = frozenset({
    "current_milestone",
    "ball_with",
    "next_action",
    "project_status",
    "target_go_live",
    "sla_limit",
    "project_tier",
})
logger = logging.getLogger(__name__)


def _as_bool(v: Any, default: Any = False) -> Any:
    """Coerce LLM-supplied boolean-ish values to a real bool.

    Local LLMs frequently emit booleans as the strings "true"/"false" (via the
    text-recovery tool-call path); Python treats every non-empty string as
    truthy, so "false" would otherwise be read as True. ``None`` is passed
    through so optional filters can stay unset.
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def get_email_analysis_result(
    project_id: str | None = None,
    analysis_ref: str | None = None,
) -> dict | None:
    """Read one structured email result without cross-invocation fallback.

    ``analysis_ref`` is the preferred correlation key (normally the email ID).
    When either key is supplied and no exact match exists, return ``None`` rather
    than leaking another in-flight email result.
    """
    with _email_analysis_lock:
        if analysis_ref:
            for result in _email_analysis_results:
                if result.get("analysis_ref") == analysis_ref and (
                    not project_id or result.get("project_id") == project_id
                ):
                    return copy.deepcopy(result)
            # Backward compatibility for legacy tool payloads that predate
            # analysis_ref. Scope the fallback to one project and one blank-ref
            # result so it cannot select another correlated invocation.
            if project_id:
                legacy = [
                    result
                    for result in _email_analysis_results
                    if result.get("project_id") == project_id
                    and not result.get("analysis_ref")
                ]
                if len(legacy) == 1:
                    return copy.deepcopy(legacy[0])
            return None
        if project_id:
            for result in _email_analysis_results:
                if result.get("project_id") == project_id:
                    return copy.deepcopy(result)
            return None
        return copy.deepcopy(_email_analysis_results[0]) if _email_analysis_results else None


def clear_email_analysis_result(
    project_id: str | None = None,
    analysis_ref: str | None = None,
) -> None:
    """Clear only the selected email-analysis scope when a key is provided."""
    with _email_analysis_lock:
        if analysis_ref:
            def matches_scope(result: dict) -> bool:
                return (
                    (not project_id or result.get("project_id") == project_id)
                    and result.get("analysis_ref") in {analysis_ref, "", None}
                )

            _email_analysis_results[:] = [
                result for result in _email_analysis_results
                if not matches_scope(result)
            ]
            for key, result in list(_email_analysis_idempotency_results.items()):
                if matches_scope(result):
                    _email_analysis_idempotency_results.pop(key, None)
        elif project_id:
            _email_analysis_results[:] = [
                result
                for result in _email_analysis_results
                if result.get("project_id") != project_id
            ]
            for key, result in list(_email_analysis_idempotency_results.items()):
                if result.get("project_id") == project_id:
                    _email_analysis_idempotency_results.pop(key, None)
        else:
            _email_analysis_results.clear()
            _email_analysis_idempotency_results.clear()


def _safe_email_text(value: Any, *, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_length]


def _safe_email_confidence(value: Any) -> float:
    try:
        confidence = float(value) if value is not None else 0.8
    except (TypeError, ValueError):
        return 0.8
    if not math.isfinite(confidence):
        return 0.8
    return min(max(confidence, 0.0), 1.0)


def _normalize_email_proposed_fields(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, field_value in value.items():
        if not isinstance(key, str) or key not in _EMAIL_PROPOSABLE_FIELDS:
            continue
        if field_value is None or isinstance(field_value, (dict, list, tuple, set)):
            continue
        normalized[key] = field_value
    return normalized


def _normalize_email_assessment_detail(
    value: Any,
    project_id: str,
    analysis_ref: str,
) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        logger.warning("Dropped email assessment detail: expected object")
        return None
    try:
        from pydantic import ValidationError
        from src.models.progress_assessment import ProjectStateAssessment

        assessment = ProjectStateAssessment.model_validate(value)
    except ValidationError as exc:
        diagnostics: list[str] = []
        for error in exc.errors(include_url=False, include_input=False):
            location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
            error_type = str(error.get("type") or "validation_error")
            item = f"{location}:{error_type}"
            if item not in diagnostics:
                diagnostics.append(item)
            if len(diagnostics) >= 12:
                break
        logger.warning(
            "Dropped invalid email assessment detail; validation_errors=%s",
            ",".join(diagnostics) or "unknown",
        )
        return None
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Dropped invalid email assessment detail (%s)",
            type(exc).__name__,
        )
        return None
    if project_id and assessment.project_id and assessment.project_id != project_id:
        logger.warning("Dropped email assessment detail: project identity mismatch")
        return None
    if analysis_ref and assessment.source_ref != analysis_ref:
        logger.warning("Dropped email assessment detail: analysis reference mismatch")
        return None
    return assessment.json_payload()


def chat_report_email_analysis(conn: sqlite3.Connection, args: dict) -> dict:
    """Agent reports structured analysis of an inbound email."""
    project_id = _safe_email_text(args.get("project_id"), max_length=200)
    analysis_ref = _safe_email_text(args.get("analysis_ref"), max_length=500)
    from src.email.analysis_execution import active_analysis_policy
    policy = active_analysis_policy()
    if policy is not None:
        policy.authorize_arguments("report_email_analysis", {"project_id": project_id, "analysis_ref": analysis_ref})
    result = {
        "project_id": project_id,
        "analysis_ref": analysis_ref,
        "summary": _safe_email_text(args.get("summary"), max_length=4000),
        "suggested_action": _safe_email_text(
            args.get("suggested_action"), max_length=4000,
        ),
        "confidence": _safe_email_confidence(args.get("confidence")),
        "proposed_fields": _normalize_email_proposed_fields(
            args.get("proposed_fields"),
        ),
        "commitment_detected": _as_bool(
            args.get("commitment_detected"), default=False,
        ),
        "email_intent": _safe_email_text(args.get("email_intent"), max_length=100),
    }
    detail = _normalize_email_assessment_detail(
        args.get("assessment_detail"), project_id, analysis_ref,
    )
    if detail is not None:
        result["assessment_detail"] = detail
    idempotency_key = _safe_email_text(
        args.get("_idempotency_key"), max_length=200,
    )
    with _email_analysis_lock:
        prior_result = (
            _email_analysis_idempotency_results.get(idempotency_key)
            if idempotency_key
            else None
        )
        if prior_result is not None:
            result = copy.deepcopy(prior_result)
        else:
            _email_analysis_results.insert(0, result)
            del _email_analysis_results[_EMAIL_ANALYSIS_MAX:]
            if idempotency_key:
                _email_analysis_idempotency_results[idempotency_key] = copy.deepcopy(result)
                while len(_email_analysis_idempotency_results) > _EMAIL_ANALYSIS_MAX:
                    oldest_key = next(iter(_email_analysis_idempotency_results))
                    _email_analysis_idempotency_results.pop(oldest_key, None)
    fields_applied = list(result["proposed_fields"].keys()) if result["proposed_fields"] else []
    from src.utils.operator_language import read_response_language

    lang = read_response_language(conn)
    pid = result["project_id"]
    summary = result["summary"]
    if lang == "en":
        msg = f"✅ Analyzed email for {pid}. {summary}"
        if fields_applied:
            msg += f" (proposed updates: {', '.join(fields_applied)})"
    else:
        msg = f"✅ 已分析 {pid} 郵件。{summary}"
        if fields_applied:
            msg += f"（建議更新：{', '.join(fields_applied)}）"
    return {
        "status": "recorded",
        "message": msg,
    }


def _format_write_result(wr, project_id: str) -> str:
    """Format a WriteResult into human-readable text for the agent."""
    if wr.outcome == "accepted":
        applied = ", ".join(f"{k} → {v}" for k, v in wr.applied_fields.items())
        return f"Updated {project_id}: {applied}"
    if wr.outcome == "no_change":
        return f"No changes needed for {project_id} (values already match)."
    if wr.outcome == "needs_review":
        return f"Update queued for review for {project_id}: {wr.needs_review_reason}"
    # rejected
    return f"Update rejected for {project_id}: {wr.rejection_reason}"


def chat_query_projects(conn: sqlite3.Connection, args: dict) -> dict:
    from src.portfolio.aggregate_query import query_projects as _qp

    include_all = _as_bool(args.pop("include_all", False))
    search_text = args.pop("search_text", None)
    min_days_stale = args.pop("min_days_stale", None)
    order_by_staleness = _as_bool(args.pop("order_by_staleness", False))
    milestone_gte = args.pop("milestone_gte", None)
    has_overdue_followups = _as_bool(args.pop("has_overdue_followups", None), None)
    filters = {k: v for k, v in args.items() if v} or None

    display_limit = 50 if include_all else 20
    result = _qp(
        conn,
        filters=filters,
        search_text=search_text,
        limit=display_limit,
        exclude_terminal_statuses=not include_all and (filters is None or "project_status" not in filters),
        min_days_stale=min_days_stale,
        order_by_staleness=order_by_staleness,
        milestone_gte=milestone_gte,
        has_overdue_followups=has_overdue_followups,
    )
    if not result.results:
        return {"text": "No projects found matching those filters."}
    lines = [f"Found {result.total} project(s):"]
    for p in result.results:
        lines.append(
            f"  {p['project_id']} - {p['client_name']} | "
            f"Status: {p['project_status']} | Ball: {p['ball_with']} | "
            f"Milestone: {p['current_milestone']} | "
            f"Updated: {str(p.get('updated_at') or '')[:10]} | "
            f"Next: {p.get('next_action') or 'Not set'}"
        )
    return {"text": "\n".join(lines)}


def _contract_status_lines(conn: sqlite3.Connection, project, assessment_detail=None) -> list[str]:
    """Render independent dimensions from deterministic structured evaluation."""
    from src.agent.progress_evaluation import evaluate_progress_detail, priority_criterion
    from src.models.progress_assessment import AssessmentCode

    evidence = conn.execute(
        """SELECT source_ref, evidence_at FROM evidence_store WHERE project_id = ?
           ORDER BY evidence_at DESC LIMIT 1""", (project.project_id,),
    ).fetchone()
    freshness = (f"latest {str(evidence['evidence_at'])[:19]} ({evidence['source_ref'] or 'no source ref'})"
                 if evidence else "no recent stored evidence")
    assessment = evaluate_progress_detail(conn, project.project_id, assessment_detail)
    if assessment is None:
        criteria_line = "structured facts unavailable; criteria not evaluated"
        priority_line = "Priority gap: unknown (abstain until structured citations are available)"
        candidate_line = "Candidate: none (abstain)"
    else:
        criteria_line = "; ".join(
            f"{item.criterion_id}={item.result.value}" for item in assessment.milestone_criteria
        ) or "no normal exit criteria"
        later_stage = AssessmentCode.INVALID_STATE_TRANSITION in (
            set(assessment.assessment_codes)
            | set(
                assessment.milestone_assessment.codes
                if assessment.milestone_assessment else []
            )
        )
        priority = priority_criterion(assessment)
        if later_stage:
            reason = ""
            if assessment.milestone_assessment and assessment.milestone_assessment.reasons:
                reason = assessment.milestone_assessment.reasons[0]
            priority_line = (
                "Priority conflict: recorded milestone lags later-stage email evidence"
                + (f" ({reason})" if reason else "")
            )
        elif priority and priority.result.value == "contradicted":
            priority_line = f"Priority conflict: {priority.criterion_id}"
        elif priority:
            priority_line = f"Priority gap: {priority.criterion_id}"
        else:
            priority_line = "Priority gap: none"
        candidate = assessment.milestone_assessment.candidate_value
        candidate_line = (f"Candidate: {candidate} (needs operator review)" if candidate
                          else "Candidate: none (abstain)")
        if candidate and candidate != project.current_milestone.value:
            candidate_line += (f"; DB truth difference: current_milestone "
                               f"{project.current_milestone.value} -> {candidate}")
        elif later_stage:
            candidate_line += "; DB truth difference: later-stage evidence vs recorded milestone"

    milestone_policy = ""
    if project.current_milestone == Milestone.M8_GO_LIVE:
        milestone_policy = "; Milestone policy: M8 does not regress for lower-layer blockers"
    return [
        f"1. Milestone & evidence freshness: Milestone: {project.current_milestone.value}; {freshness}{milestone_policy}",
        f"   Criteria: {criteria_line}", f"   {priority_line}", f"   {candidate_line}",
        f"2. Project status / health: Status: {project.project_status.value}",
        f"3. Ball-with & obligation: Ball with: {project.ball_with.value}; obligation: {project.next_action or 'none'}",
        f"4. Next action: {project.next_action or 'N/A'}",
    ]


def chat_get_project_status(conn: sqlite3.Connection, args: dict) -> dict:
    project = project_repo.get_project(conn, args["project_id"])
    if not project:
        return {"text": f"Project {args['project_id']} not found."}

    _updated_at_line = "Last updated: N/A"
    if project.updated_at is not None:
        try:
            from datetime import datetime as _dt
            updated_at_dt = project.updated_at.replace(tzinfo=None)
            days_since = (_dt.utcnow() - updated_at_dt).days
            _updated_at_line = f"Last updated: {updated_at_dt.strftime('%Y-%m-%d')} ({days_since}d ago)"
        except (AttributeError, TypeError):
            pass

    lines = [
        f"Project: {project.project_id}",
        f"Client: {project.client_name}",
        _updated_at_line,
        *_contract_status_lines(conn, project, args.get("assessment_detail")),
        f"Target Go-Live: {project.target_go_live or 'N/A'}",
        f"Notes: {(project.notes or 'N/A')[:200]}",
    ]

    extras = []

    from src.context.working_memory_display import enrich_memory_entry
    from src.repositories.working_memory_repo import get_memory
    mem = get_memory(conn, project.project_id, "latest_note")
    if mem and mem.get("memory_value"):
        mem = enrich_memory_entry(
            mem, current_milestone=project.current_milestone,
        )
        stale = " [stale]" if mem.get("is_stale") else ""
        if mem.get("stale_reason") == "milestone_superseded":
            stale = " [stale: milestone superseded]"
        date_part = (mem.get("updated_at") or "")[:10]
        extras.append(
            f"Latest note ({date_part}){stale} — may not match current milestone; "
            f"see Milestone/Next action above: {mem['memory_value'][:280]}"
        )

    if project.target_go_live:
        from datetime import date
        try:
            target_date = date.fromisoformat(str(project.target_go_live)[:10])
            days_remaining = (target_date - date.today()).days
            if days_remaining < 0:
                extras.append(f"SLA: Go-live target {project.target_go_live} (OVERDUE by {-days_remaining} days)")
            else:
                extras.append(f"SLA: Go-live target {project.target_go_live} ({days_remaining} days remaining)")
        except ValueError:
            pass

    try:
        fup_rows = conn.execute(
            """SELECT status FROM internal_followups
               WHERE project_id = ? AND status IN ('open', 'overdue')""",
            (project.project_id,),
        ).fetchall()
        if fup_rows:
            overdue_count = sum(1 for r in fup_rows if r["status"] == "overdue")
            extras.append(f"Open follow-ups: {len(fup_rows)} ({overdue_count} overdue)")
    except Exception:
        pass

    try:
        from src.fix_sessions.repository import list_candidates, list_sessions

        sessions = list_sessions(conn, project.project_id)
        if sessions:
            session_lines = ["Verified FIX session configuration:"]
            for session in sessions:
                endpoints = ", ".join(
                    f"{ep['route_role']}={ep['host']}:{ep['port']}"
                    for ep in session.get("endpoints", [])
                )
                session_lines.append(
                    f"- {session['environment']} / {session['session_key']}: "
                    f"SenderCompID={session['sender_comp_id']}, "
                    f"TargetCompID={session['target_comp_id']}, {endpoints}"
                )
            extras.append("\n".join(session_lines))
        pending = list_candidates(conn, project.project_id)
        if pending:
            extras.append(
                f"Pending FIX configuration: {len(pending)} candidate(s) awaiting confirmation; "
                "unverified values are not shown as authoritative."
            )
    except Exception:
        pass

    try:
        from src.email.progress_backfill import (
            format_progress_conflict_block,
            load_email_progress_recommendation,
        )

        progress_block = format_progress_conflict_block(
            load_email_progress_recommendation(
                conn,
                project.project_id,
                current_next_action=project.next_action,
            )
        )
        if progress_block:
            extras.append(progress_block)
    except Exception:
        pass

    if extras:
        lines.append("---")
        lines.extend(extras)

    return {"text": "\n".join(lines)}


def _read_back_project_state(conn: sqlite3.Connection, project_id: str) -> str:
    """Return a one-line snapshot of current DB state for read-after-write verification."""
    row = conn.execute(
        "SELECT current_milestone, project_status, ball_with FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if not row:
        return ""
    return (
        f" [DB state after write: milestone={row['current_milestone']}, "
        f"status={row['project_status']}, ball_with={row['ball_with']}]"
    )


_PENDING_KEY = "pending_chat_update"
_LEGACY_CLEANUP_ERRORS = frozenset({
    "invalid_pending_update", "stale_typed_pending_memory",
})


def _stage_chat_update(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
    reason: str,
    evidence_ref: str | None,
    *,
    operator_id: str = "default_operator",
    chat_id: str = "legacy",
) -> dict:
    """Validate and stage an update without writing to DB. Returns diff for operator review."""
    from src.repositories.project_repo import get_project_field
    from src.write_layer.write_helpers import (
        fields_unchanged,
        normalize_and_validate_enums,
        validate_field_names,
        validate_milestone_transition,
        validate_status_transition,
    )

    # Step 0: terminal status check
    ok, err = validate_status_transition(conn, project_id)
    if not ok:
        return {"text": f"Update rejected for {project_id}: {err}"}

    # Step 1: field name validation
    ok, err = validate_field_names(fields)
    if not ok:
        return {"text": f"Update rejected for {project_id}: {err}"}

    # Step 2: enum normalization
    ok, err = normalize_and_validate_enums(fields)
    if not ok:
        return {"text": f"Update rejected for {project_id}: {err}"}

    # Step 3: milestone transition check
    if "current_milestone" in fields:
        ok, err = validate_milestone_transition(
            conn, project_id, fields["current_milestone"], evidence_ref=evidence_ref,
        )
        if not ok:
            return {"text": f"Update rejected for {project_id}: {err}"}

    # Step 4: filter no-ops
    changed = fields_unchanged(conn, project_id, fields)
    if not changed:
        return {"text": f"No changes needed for {project_id} (values already match)."}

    # Build diff for display
    diff_lines = []
    for k, new_val in changed.items():
        old_val = get_project_field(conn, project_id, k)
        diff_lines.append(f"  {k}: {old_val} → {new_val}")
    diff_text = "\n".join(diff_lines)

    staged = {
        "fields": changed,
        "old_values": {
            key: get_project_field(conn, project_id, key) for key in changed
        },
        "reason": reason,
        "evidence_ref": evidence_ref,
    }
    row = conn.execute(
        "SELECT updated_at FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    base_version = row[0] if row else None
    from src.services.project_update_commands import stage_project_update_command

    card_queued = False
    with savepoint(conn, ensure_transaction=True):
        operation = stage_project_update_command(
            conn,
            operator_id=operator_id,
            chat_id=chat_id,
            project_id=project_id,
            payload=staged,
            base_version=base_version,
        )
        # Transitional compatibility for existing cards/guards. Typed operations
        # are authoritative; legacy memory remains readable for one release.
        upsert_memory(conn, project_id, _PENDING_KEY, json.dumps({
            **staged,
            "operation_id": operation.operation_id,
        }))
        # Numeric chats belong to Telegram. Record the card intent alongside
        # the operation, before the tool's writer scope can commit. The later
        # response guard may call the same idempotent enqueue for compatibility.
        if chat_id.isdecimal():
            from src.telegram.staged_update_push import enqueue_staged_update_card

            card_queued = enqueue_staged_update_card(
                conn, project_id=project_id, chat_id=int(chat_id),
            ).get("queued", False)

    return {
        "ok": True,
        "outcome": "staged",
        "card_queued": bool(card_queued),
        "committed": False,
        "operation_id": operation.operation_id,
        "project_id": project_id,
        "expires_at": operation.expires_at,
        "text": (
            f"Proposed changes for {project_id}:\n{diff_text}\n\n"
            f"Operation {operation.operation_id}. Call confirm_update to apply "
            "or cancel_update to discard."
        ),
    }


def chat_update_project(conn: sqlite3.Connection, args: dict) -> dict:
    project_id = args["project_id"]
    field = args["field"]
    new_value = args["new_value"]
    reason = args.get("reason", "operator instruction")
    evidence_ref = args.get("evidence_ref") or None
    if field == "current_milestone" and not args.get("_trusted_explicit_operator"):
        from src.agent.progress_evaluation import evaluate_progress_detail
        assessment = evaluate_progress_detail(conn, project_id)
        candidate = assessment.milestone_assessment.candidate_value if assessment else None
        if candidate != new_value:
            return {"ok": False, "outcome": "rejected", "committed": False,
                    "text": f"Update rejected for {project_id}: deterministic evaluator abstained."}
    return _stage_chat_update(
        conn, project_id, {field: new_value}, reason, evidence_ref,
        operator_id=str(args.get("_operator_id") or "default_operator"),
        chat_id=str(args.get("chat_id") or "legacy"),
    )


def chat_update_project_batch(conn: sqlite3.Connection, args: dict) -> dict:
    project_id = args.get("project_id", "")
    reason = args.get("reason", "batch update")
    evidence_ref = args.get("evidence_ref") or None
    changes = args.get("changes", [])
    if not changes:
        return {"text": "Error: changes list is empty."}
    fields = {c.get("field", ""): c.get("new_value") for c in changes}
    if "current_milestone" in fields and not args.get("_trusted_explicit_operator"):
        from src.agent.progress_evaluation import evaluate_progress_detail
        assessment = evaluate_progress_detail(conn, project_id)
        candidate = assessment.milestone_assessment.candidate_value if assessment else None
        if candidate != fields["current_milestone"]:
            return {"ok": False, "outcome": "rejected", "committed": False,
                    "text": f"Update rejected for {project_id}: deterministic evaluator abstained."}
    return _stage_chat_update(
        conn, project_id, fields, reason, evidence_ref,
        operator_id=str(args.get("_operator_id") or "default_operator"),
        chat_id=str(args.get("chat_id") or "legacy"),
    )


def _pending_operation_error(error: str, text: str) -> dict:
    """Build a consistent non-committed pending-operation failure."""
    return {
        "ok": False,
        "error": error,
        "committed": False,
        "text": text,
    }


def _load_genuine_legacy_staged_update(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[dict | None, dict | None]:
    """Load only pre-typed-operation memory; reject stale typed compatibility memory."""
    from src.repositories.pending_operation_repo import get_operation

    def _clear_invalid(error: str, text: str) -> tuple[None, dict]:
        # A cleanup failure must not leave an incomplete command alongside
        # unrelated caller edits. The caller still owns commit/rollback.
        with savepoint(conn, ensure_transaction=True):
            delete_memory(conn, project_id, _PENDING_KEY)
        return None, _pending_operation_error(error, text)

    mem = get_memory(conn, project_id, _PENDING_KEY)
    if not mem:
        return None, _pending_operation_error(
            "no_pending_update",
            f"No pending update for {project_id}.",
        )
    try:
        staged = json.loads(mem["memory_value"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return _clear_invalid(
            "invalid_pending_update",
            f"The staged update for {project_id} is invalid and was cleared.",
        )
    if not isinstance(staged, dict):
        return _clear_invalid(
            "invalid_pending_update",
            f"The staged update for {project_id} is invalid and was cleared.",
        )

    typed_operation_id = str(staged.get("operation_id") or "").strip()
    if typed_operation_id:
        typed_operation = get_operation(conn, typed_operation_id)
        typed_status = typed_operation.status if typed_operation is not None else "missing"
        return _clear_invalid(
            "stale_typed_pending_memory",
            (
                f"The compatibility record for {project_id} points to typed operation "
                f"{typed_operation_id}, which is {typed_status}. Re-check and stage the update again."
            ),
        )

    fields = staged.get("fields")
    if not isinstance(fields, dict) or not fields:
        return _clear_invalid(
            "invalid_pending_update",
            f"The staged update for {project_id} has no valid fields and was cleared.",
        )
    return staged, None


def chat_confirm_update(conn: sqlite3.Connection, args: dict) -> dict:
    """Apply an owned, unexpired staged update after operator confirmation."""
    from src.governance.four_eyes import (
        is_high_risk_project_fields,
        team_four_eyes_enabled,
    )
    from src.repositories.pending_operation_repo import (
        get_operation,
        list_pending_operations,
        list_project_chat_pending_operations,
    )
    from src.services.project_update_commands import (
        confirm_project_update_command, publish_confirmed_project_update,
    )
    from src.write_layer.write_helpers import safe_write_project

    project_id = args["project_id"]
    operator_id = str(args.get("_operator_id") or "default_operator")
    chat_id = str(args.get("chat_id") or "legacy")
    operation_id = str(args.get("operation_id") or "").strip()
    commit = _as_bool(args.get("commit"), True)
    publish_event = _as_bool(args.get("publish_event"), True)
    if publish_event and not commit:
        raise ValueError("publish_event=True requires commit=True")

    operation = None
    if operation_id:
        operation = get_operation(conn, operation_id)
        if operation is None:
            return _pending_operation_error(
                "pending_operation_not_found",
                f"Operation {operation_id} was not found.",
            )
    else:
        candidates = list_pending_operations(
            conn,
            operator_id=operator_id,
            chat_id=chat_id,
            project_id=project_id,
            operation_type="project_update",
        )
        if not candidates and team_four_eyes_enabled():
            candidates = list_project_chat_pending_operations(
                conn,
                chat_id=chat_id,
                project_id=project_id,
            )
            candidates = [
                item
                for item in candidates
                if is_high_risk_project_fields(
                    conn, project_id, (item.payload or {}).get("fields") or {}
                )[0]
            ]
        if len(candidates) == 1:
            operation = candidates[0]
        elif len(candidates) > 1:
            return _pending_operation_error(
                "ambiguous_pending_operation",
                f"Multiple pending updates exist for {project_id}; specify operation_id.",
            )

    if operation is not None:
        command_result = confirm_project_update_command(
            conn,
            project_id=project_id,
            operator_id=operator_id,
            chat_id=chat_id,
            operation_id=operation.operation_id,
            authority="four_eyes",
            commit=commit,
            publish_event=publish_event,
        )
        if not command_result.ok or command_result.write_result is None:
            return command_result.to_dict()
        result_text = _format_write_result(command_result.write_result, project_id)
        result_text += _read_back_project_state(conn, project_id)
        receipt = command_result.to_dict(text=result_text)
        receipt["applied_fields"] = dict(command_result.applied_fields)
        receipt["surface"] = str(command_result.payload.get("surface") or "telegram")
        effects = args.get("_post_commit_effects")
        if command_result.outcome == "accepted" and isinstance(effects, list):
            applied = dict(command_result.applied_fields)
            surface = receipt["surface"]
            effects.append(lambda: publish_confirmed_project_update(
                project_id, applied, surface=surface,
            ))
        return receipt

    caller_in_transaction = conn.in_transaction
    staged, legacy_error = _load_genuine_legacy_staged_update(conn, project_id)
    if legacy_error is not None:
        if commit and not caller_in_transaction and legacy_error["error"] in _LEGACY_CLEANUP_ERRORS:
            conn.commit()
        return legacy_error
    assert staged is not None

    surface = str(staged.get("surface") or "telegram")
    with savepoint(conn, ensure_transaction=True):
        wr = safe_write_project(
            conn,
            project_id,
            staged["fields"],
            actor=operator_id,
            reason=staged.get("reason") or "operator confirmation",
            evidence_ref=staged.get("evidence_ref"),
            operator_confirmed=True,
            surface=surface,
            commit=False,
            publish_event=False,
            schedule_notifications=False,
            rollback_on_error=False,
        )
        if commit and caller_in_transaction and wr.outcome in ("accepted", "no_change"):
            raise ValueError("commit=True cannot own an existing transaction; use commit=False")
        if wr.outcome in ("accepted", "no_change"):
            delete_memory(conn, project_id, _PENDING_KEY)
    if commit and (wr.outcome in ("accepted", "no_change") or not caller_in_transaction):
        conn.commit()

    if commit and wr.outcome == "accepted":
        publish_confirmed_project_update(
            project_id, dict(wr.applied_fields), surface=surface,
            publish_event=publish_event,
        )

    if wr.outcome == "accepted" and isinstance(args.get("_post_commit_effects"), list):
        applied = dict(wr.applied_fields)
        args["_post_commit_effects"].append(
            lambda: publish_confirmed_project_update(
                project_id, applied, surface=surface,
            )
        )

    result_text = _format_write_result(wr, project_id)
    result_text += _read_back_project_state(conn, project_id)
    return {
        "ok": wr.outcome in ("accepted", "no_change"),
        "outcome": wr.outcome,
        "committed": commit and wr.outcome in ("accepted", "no_change"),
        "applied_fields": dict(wr.applied_fields),
        "surface": surface,
        "operation_id": staged.get("operation_id"),
        "project_id": project_id,
        "text": result_text,
    }

def chat_cancel_update(conn: sqlite3.Connection, args: dict) -> dict:
    """Discard an owned, unexpired pending update."""
    from src.repositories.pending_operation_repo import (
        get_operation,
        list_pending_operations,
    )
    from src.services.project_update_commands import cancel_project_update_command

    project_id = args["project_id"]
    operator_id = str(args.get("_operator_id") or "default_operator")
    chat_id = str(args.get("chat_id") or "legacy")
    operation_id = str(args.get("operation_id") or "").strip()
    commit = _as_bool(args.get("commit"), True)

    operation = None
    if operation_id:
        operation = get_operation(conn, operation_id)
        if operation is None:
            return _pending_operation_error(
                "pending_operation_not_found",
                f"Operation {operation_id} was not found.",
            )
    else:
        candidates = list_pending_operations(
            conn,
            operator_id=operator_id,
            chat_id=chat_id,
            project_id=project_id,
            operation_type="project_update",
        )
        if len(candidates) == 1:
            operation = candidates[0]
        elif len(candidates) > 1:
            return _pending_operation_error(
                "ambiguous_pending_operation",
                f"Multiple pending updates exist for {project_id}; specify operation_id.",
            )

    if operation is not None:
        return cancel_project_update_command(
            conn,
            project_id=project_id,
            operator_id=operator_id,
            chat_id=chat_id,
            operation_id=operation.operation_id,
            commit=commit,
        ).to_dict()

    caller_in_transaction = conn.in_transaction
    staged, legacy_error = _load_genuine_legacy_staged_update(conn, project_id)
    if legacy_error is not None:
        if commit and not caller_in_transaction and legacy_error["error"] in _LEGACY_CLEANUP_ERRORS:
            conn.commit()
        return legacy_error
    assert staged is not None
    if commit and caller_in_transaction:
        raise ValueError("commit=True cannot own an existing transaction; use commit=False")
    with savepoint(conn, ensure_transaction=True):
        delete_memory(conn, project_id, _PENDING_KEY)
    if commit:
        conn.commit()
    return {
        "ok": True,
        "outcome": "cancelled",
        "committed": commit,
        "project_id": project_id,
        "text": f"Staged update for {project_id} discarded.",
    }

def chat_undo_update(conn: sqlite3.Connection, args: dict) -> dict:
    """Undo the most recent accepted write by reverting fields to their old values."""
    from src.repositories.activity_log_repo import insert_activity
    from src.write_layer.write_helpers import safe_write_project

    project_id = args["project_id"]
    row = conn.execute(
        """SELECT proposed_changes FROM proposed_updates
           WHERE project_id = ? AND decision_outcome = 'accepted'
           ORDER BY decided_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    if not row:
        return {"text": f"No revertible write found for {project_id}."}
    changes = json.loads(row["proposed_changes"])
    revert_dict = {
        c["field"]: c["old_value"]
        for c in changes if c.get("old_value") is not None
    }
    if not revert_dict:
        return {"text": f"No revertible fields found for {project_id} (no old values recorded)."}
    wr = safe_write_project(
        conn, project_id, revert_dict,
        actor="operator", reason="undo", operator_confirmed=True,
    )
    insert_activity(
        conn, actor="operator", action_type="undo",
        project_id=project_id,
        summary=f"Undo: reverted {', '.join(revert_dict.keys())}",
    )
    result = _format_write_result(wr, project_id)
    result += _read_back_project_state(conn, project_id)
    return {"text": result}


def chat_add_contact(conn: sqlite3.Connection, args: dict) -> dict:
    email = args.get("email", "").strip()
    if not email:
        return {"text": "Error: email is required."}
    display_name = args.get("name") or email.split("@")[0]
    project_id = args.get("project_id")
    role = args.get("role")
    result = contact_repo.upsert_contact_for_project(conn, email, display_name, project_id, role=role)
    action = result["action"]
    return {
        "ok": True,
        "outcome": "accepted",
        "committed": True,
        "operation_id": result["contact_id"],
        "project_id": project_id,
        "contact_id": result["contact_id"],
        "action": action,
        "text": f"Contact {email} {action} (ID: {result['contact_id']}) and linked to {project_id}.",
    }


def chat_list_contacts(conn: sqlite3.Connection, args: dict) -> dict:
    project_id = args.get("project_id", "")
    rows = conn.execute(
        """SELECT display_name, primary_email, role, created_by
           FROM contacts
           WHERE cross_project_history LIKE ?
           ORDER BY display_name""",
        (f"%{project_id}%",),
    ).fetchall()
    if not rows:
        return {"text": f"No contacts registered for {project_id}."}
    lines = [f"Contacts for {project_id}:"]
    for r in rows:
        d = dict(r)
        conf = "confirmed" if d.get("created_by") == "operator_confirmed" else "auto"
        lines.append(f"  {d.get('display_name') or '?'} <{d.get('primary_email')}> role={d.get('role') or '-'} [{conf}]")
    return {"text": "\n".join(lines)}


def chat_add_note(conn: sqlite3.Connection, args: dict) -> dict:
    from src.notes.note_service import store_note

    project_id = args.get("project_id", "")
    note = args.get("note", "").strip()
    if not note:
        return {"text": "Error: note text is required."}
    store_note(conn, project_id, note, source="operator_telegram")
    return {"text": f"Note recorded for {project_id}."}


def chat_manage_followup(conn: sqlite3.Connection, args: dict) -> dict:
    from src.portfolio.followup_tracker import (
        cancel_followup, complete_followup, create_followup, get_open_followups,
    )

    action = args.get("action", "").strip()
    project_id = args.get("project_id", "")
    from src.email.analysis_execution import active_analysis_policy
    policy = active_analysis_policy()
    if policy is not None:
        policy.authorize_arguments("manage_followup", {"action": action, "project_id": project_id})
        if action == "create":
            desc = args.get("description", "").strip()
            due_date = args.get("due_date", "").strip()
            if not desc:
                return {"text": "Error: description is required to create a follow-up."}
            policy.stage_followup(project_id=project_id, description=desc, due_date=due_date or None)
            return {"text": f"Follow-up staged for {project_id}; it will be created only if the inbound email command commits: {desc}"}

    if action == "list":
        items = get_open_followups(conn, project_id)
        if not items:
            return {"text": f"No open follow-ups for {project_id}."}
        lines = [f"Open follow-ups for {project_id}:"]
        for fup in items:
            lines.append(f"  [{fup['followup_id']}] rev={fup.get('action_revision', 0)} {fup['description']} - due {fup.get('due_date') or '?'}")
        return {"text": "\n".join(lines)}

    if action == "create":
        desc = args.get("description", "").strip()
        due_date = args.get("due_date", "").strip()
        if not desc:
            return {"text": "Error: description is required to create a follow-up."}
        fid = create_followup(conn, project_id=project_id, description=desc, due_date=due_date or None, created_by="operator")
        return {"text": f"Follow-up created (ID: {fid}) for {project_id}: {desc}"}

    if action == "complete":
        fid = args.get("followup_id", "").strip()
        if not fid:
            return {"text": "Error: followup_id is required to complete a task."}
        expected_revision = args.get("expected_revision")
        if expected_revision is None:
            return {"text": "Error: expected_revision is required; list follow-ups again before completing."}
        ok = complete_followup(conn, fid, resolution_note=args.get("resolution_note", ""), expected_revision=int(expected_revision))
        return {"text": f"Follow-up {fid} marked complete." if ok else f"Follow-up {fid} not found or changed; list follow-ups again."}

    if action == "cancel":
        fid = args.get("followup_id", "").strip()
        if not fid:
            return {"text": "Error: followup_id is required to cancel a task."}
        expected_revision = args.get("expected_revision")
        if expected_revision is None:
            return {"text": "Error: expected_revision is required; list follow-ups again before cancelling."}
        ok = cancel_followup(conn, fid, expected_revision=int(expected_revision))
        return {"text": f"Follow-up {fid} cancelled." if ok else f"Follow-up {fid} not found or changed; list follow-ups again."}

    return {"text": f"Unknown manage_followup action: {action}. Use create, complete, cancel, or list."}


def chat_get_activity_log(conn: sqlite3.Connection, args: dict) -> dict:
    """Get recent activity log for a project."""
    from src.repositories.activity_log_repo import get_recent_activity

    project_id = args.get("project_id", "")
    limit = min(int(args.get("limit", 20)), 50)
    action_type = args.get("action_type")
    entries = get_recent_activity(conn, project_id, limit=limit)
    if action_type:
        entries = [e for e in entries if e.get("action_type") == action_type]
    if not entries:
        return {"text": f"No activity found for {project_id}."}
    lines = [f"Activity log for {project_id} ({len(entries)} entries):"]
    for e in entries:
        lines.append(
            f"  [{e.get('created_at', '?')[:16]}] {e.get('action_type', '?')} "
            f"by {e.get('actor', '?')}: {e.get('summary', '')}"
        )
    return {"text": "\n".join(lines)}


from src.tools.draft_handlers import (  # noqa: F401
    chat_discard_draft,
    chat_edit_draft,
    chat_get_draft,
    chat_list_drafts,
)

CHAT_TOOL_HANDLERS: dict[str, Any] = {
    "query_projects": chat_query_projects,
    "get_project_status": chat_get_project_status,
    "update_project": chat_update_project,
    "update_project_batch": chat_update_project_batch,
    "confirm_update": chat_confirm_update,
    "cancel_update": chat_cancel_update,
    "undo_update": chat_undo_update,
    "add_contact": chat_add_contact,
    "list_contacts": chat_list_contacts,
    "add_note": chat_add_note,
    "manage_followup": chat_manage_followup,
    "get_activity_log": chat_get_activity_log,
    "list_drafts": chat_list_drafts,
    "get_draft": chat_get_draft,
    "edit_draft": chat_edit_draft,
    "discard_draft": chat_discard_draft,
    "report_email_analysis": chat_report_email_analysis,
}
