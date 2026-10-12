"""Preview / validate a Workspace operator milestone update."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from src.agent.progress_evaluation import evaluate_progress_detail
from src.governance.four_eyes import is_high_risk_project_fields
from src.milestone.contracts import get_milestone_contract
from src.milestone.state_machine import MILESTONE_SEQUENCE, is_revert_transition
from src.models.enums import Milestone
from src.models.progress_assessment import AssessmentDecision, CriterionResult
from src.repositories.project_repo import get_project_field
from src.services import operator_update_metrics as metrics
from src.services.operator_update_models import (
    Kind,
    OperatorUpdateError,
    OperatorUpdateProposal,
    Transition,
    V1_FIELDS,
    canonical_evidence_ref,
)
from src.write_layer.closing_guards import is_vague_next_action
from src.write_layer.write_helpers import (
    normalize_and_validate_enums,
    validate_field_names,
    validate_milestone_transition,
    validate_status_transition,
)

logger = logging.getLogger("agentfixbpro.operator_update")


def _transition_kind(from_ms: Milestone | None, to_ms: Milestone) -> Transition:
    if from_ms is None:
        return "adjacent" if to_ms == Milestone.M1_NETWORK_SETUP else "jump"
    if from_ms == to_ms:
        return "noop"
    if is_revert_transition(from_ms, to_ms):
        return "revert"
    try:
        delta = MILESTONE_SEQUENCE.index(to_ms) - MILESTONE_SEQUENCE.index(from_ms)
    except ValueError:
        return "jump"
    if delta == 1:
        return "adjacent"
    return "jump"


def _assessment_status(decision: str | None) -> str:
    if decision == AssessmentDecision.ACCEPT_CANDIDATE.value:
        return "ready"
    if decision == AssessmentDecision.NEEDS_REVIEW.value:
        return "needs_review"
    if decision is None:
        return "not_available"
    return "not_ready"


def _missing_criteria(assessment, current_milestone: str | None) -> list[dict]:
    if assessment is None or not current_milestone:
        return []
    try:
        contract = get_milestone_contract(current_milestone)
    except Exception:
        contract = None
    desc_by_id = {
        criterion.id: criterion.description
        for criterion in (contract.required_exit_criteria if contract else [])
    }
    return [
        {
            "id": item.criterion_id,
            "description": desc_by_id.get(item.criterion_id) or item.explanation,
            "result": item.result.value,
        }
        for item in assessment.milestone_criteria
        if item.result != CriterionResult.MET
    ]


def preview_operator_update(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    fields: dict[str, Any],
    reason: str = "",
    evidence_ref: str | None = None,
) -> OperatorUpdateProposal:
    project_id = project_id.strip().upper()
    exists = conn.execute(
        "SELECT 1 FROM projects WHERE project_id=?", (project_id,),
    ).fetchone()
    if exists is None:
        raise OperatorUpdateError("project_not_found", f"Project {project_id} not found.")

    unknown = [key for key in fields if key not in V1_FIELDS]
    if unknown:
        raise OperatorUpdateError(
            "field_not_in_v1",
            f"Field(s) {sorted(unknown)} are not allowed on Workspace operator updates.",
        )
    if "current_milestone" not in fields:
        raise OperatorUpdateError(
            "field_not_in_v1",
            "Workspace v1 operator updates require current_milestone.",
        )

    working = dict(fields)
    ok, err = validate_field_names(working)
    if not ok:
        raise OperatorUpdateError("field_not_in_v1", err or "Invalid fields.")
    ok, err = normalize_and_validate_enums(working)
    if not ok:
        raise OperatorUpdateError("invalid_transition", err or "Invalid enum value.")
    ok, err = validate_status_transition(conn, project_id)
    if not ok:
        raise OperatorUpdateError("invalid_transition", err or "Project is read-only.")

    current_ms_raw = get_project_field(conn, project_id, "current_milestone")
    try:
        from_ms = Milestone(current_ms_raw) if current_ms_raw else None
        to_ms = Milestone(working["current_milestone"])
    except ValueError as exc:
        raise OperatorUpdateError("invalid_transition", str(exc)) from exc
    if from_ms is not None and to_ms.index < from_ms.index:
        raise OperatorUpdateError(
            "invalid_transition",
            f"Milestones do not regress. Keep {from_ms.value} and track the corrective action at the current stage.",
        )
    transition = _transition_kind(from_ms, to_ms)
    if transition == "jump":
        raise OperatorUpdateError(
            "transition_not_in_v1",
            "Milestone jumps greater than +1 are not available in Workspace v1.",
        )
    if transition == "noop":
        raise OperatorUpdateError(
            "invalid_transition",
            f"{project_id} is already {to_ms.value}.",
        )
    ok, err = validate_milestone_transition(
        conn, project_id, working["current_milestone"], evidence_ref=evidence_ref,
    )
    if not ok:
        raise OperatorUpdateError("invalid_transition", err or "Invalid milestone transition.")

    if "next_action" in working and is_vague_next_action(str(working["next_action"])):
        raise OperatorUpdateError(
            "vague_next_action",
            "next_action is too vague; write a specific operator-typed action.",
        )

    resolved_ref = (
        canonical_evidence_ref(conn, project_id, evidence_ref) if evidence_ref else None
    )
    high_risk, high_reason = is_high_risk_project_fields(conn, project_id, working)
    assessment = evaluate_progress_detail(conn, project_id)
    decision = None
    candidate = None
    policy_version = None
    snapshot: dict[str, Any] = {}
    if assessment is not None:
        field = assessment.milestone_assessment
        decision = field.decision.value if field and field.decision else None
        candidate = field.candidate_value if field else None
        policy_version = assessment.policy_version
        snapshot = {
            "decision": decision,
            "candidate_value": candidate,
            "policy_version": policy_version,
        }
    aligned = (
        decision == AssessmentDecision.ACCEPT_CANDIDATE.value
        and candidate == working["current_milestone"]
    )
    kind: Kind = "evaluator_aligned" if aligned else "operator_override"
    row = conn.execute(
        "SELECT updated_at, next_action, project_status FROM projects WHERE project_id=?",
        (project_id,),
    ).fetchone()
    base_version = str(row["updated_at"]) if row else ""
    current_values = {
        "current_milestone": current_ms_raw,
        "next_action": row["next_action"] if row else None,
        "project_status": row["project_status"] if row else None,
    }
    metrics.increment("workspace_operator_update_preview", kind=kind, transition=transition)
    logger.info(
        "preview project_id=%s kind=%s transition=%s",
        project_id, kind, transition,
    )
    return OperatorUpdateProposal(
        project_id=project_id,
        fields={key: str(working[key]) for key in working},
        reason=" ".join((reason or "").split()).strip(),
        evidence_ref=resolved_ref,
        kind=kind,
        assessment=snapshot,
        missing_criteria=_missing_criteria(assessment, current_ms_raw),
        current_values=current_values,
        base_version=base_version,
        high_risk=bool(high_risk),
        high_risk_reason=high_reason,
        transition=transition,
        assessment_status=_assessment_status(decision),
        policy_version=policy_version,
        decision=decision,
        candidate_value=candidate,
    )
