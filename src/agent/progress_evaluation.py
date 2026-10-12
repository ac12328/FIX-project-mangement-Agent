"""Shared deterministic progress evaluation for chat status, advisory, and staging."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.milestone.contracts import load_milestone_contract_policy
from src.models.enums import BallWith, Milestone, ProjectStatus
from src.models.progress_assessment import (
    CriterionResult, EvidenceSourceType, ProjectStateAssessment,
)
from src.project_state.evaluator import ProjectStateInput, assess_project_state
from src.repositories.project_repo import get_project


def _valid_persisted_detail(proposal: dict[str, Any], project_id: str) -> dict[str, Any] | None:
    detail = proposal.get("assessment")
    if not isinstance(detail, dict):
        return None
    if (
        proposal.get("project_id") != project_id
        or not proposal.get("source_ref")
        or not proposal.get("source_type")
        or proposal.get("policy_version") != load_milestone_contract_policy().policy_version
        or detail.get("project_id") != project_id
        or detail.get("source_ref") != proposal.get("source_ref")
        or detail.get("source_type") != proposal.get("source_type")
        or detail.get("policy_version") != proposal.get("policy_version")
    ):
        return None
    return detail


def _latest_persisted_detail(
    conn,
    project_id: str,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any] | None:
    if as_of is None:
        from src.repositories.proposed_update_repo import get_proposed_updates

        proposals = get_proposed_updates(conn, project_id, limit=10)
    else:
        cutoff = as_of.astimezone(timezone.utc).isoformat()
        rows = conn.execute(
            """SELECT project_id, source_ref, source_type, policy_version, assessment_json
               FROM proposed_updates
               WHERE project_id=? AND assessment_json IS NOT NULL
                 AND datetime(COALESCE(decided_at, created_at)) <= datetime(?)
               ORDER BY datetime(COALESCE(decided_at, created_at)) DESC,
                        created_at DESC, update_id DESC
               LIMIT 20""",
            (project_id, cutoff),
        ).fetchall()
        proposals = []
        for raw in rows:
            proposal = dict(raw)
            try:
                proposal["assessment"] = json.loads(proposal.pop("assessment_json") or "null")
            except (TypeError, json.JSONDecodeError):
                proposal["assessment"] = None
            proposals.append(proposal)

    for proposal in proposals:
        detail = _valid_persisted_detail(proposal, project_id)
        if detail is not None:
            return detail
    return None


def evaluate_progress_detail(
    conn,
    project_id: str,
    detail: dict[str, Any] | None = None,
    *,
    as_of: datetime | None = None,
    project_snapshot: dict[str, Any] | None = None,
) -> ProjectStateAssessment | None:
    """Re-evaluate trusted structured facts against current or historical DB truth."""
    persisted = _latest_persisted_detail(conn, project_id, as_of=as_of)
    if persisted is not None:
        if detail is not None and detail != persisted:
            return None
        detail = persisted
    elif detail is not None:
        # Internal callers may pass a fully validated extraction payload only when
        # its citations are source-bound below. Public tool schemas do not expose it.
        detail = detail
    if not detail:
        return None
    try:
        extracted = ProjectStateAssessment.model_validate(detail)
        project = get_project(conn, project_id)
        if project is None or extracted.project_id != project_id:
            return None
        for fact in extracted.extracted_facts:
            for citation in fact.citations:
                if citation.source_type == extracted.source_type and citation.source_ref == extracted.source_ref:
                    continue
                if citation.source_type.value == "attachment":
                    prefix = f"attachment:{extracted.source_ref}:"
                    if as_of is None:
                        linked = conn.execute(
                            """SELECT 1 FROM evidence_store
                               WHERE project_id=? AND evidence_type='attachment' AND source_ref=?
                               LIMIT 1""",
                            (project_id, citation.source_ref),
                        ).fetchone()
                    else:
                        cutoff = as_of.astimezone(timezone.utc).isoformat()
                        linked = conn.execute(
                            """SELECT 1 FROM evidence_store
                               WHERE project_id=? AND evidence_type='attachment' AND source_ref=?
                                 AND datetime(evidence_at) <= datetime(?)
                               LIMIT 1""",
                            (project_id, citation.source_ref, cutoff),
                        ).fetchone()
                    if linked and citation.source_ref.startswith(prefix):
                        continue
                return None

        snapshot = project_snapshot or {}
        milestone_value = snapshot.get("current_milestone", project.current_milestone)
        status_value = snapshot.get("project_status", project.project_status)
        ball_value = snapshot.get("ball_with", project.ball_with)
        next_action = snapshot.get("next_action", project.next_action)
        return assess_project_state(ProjectStateInput(
            project_id=project_id,
            source_type=extracted.source_type or EvidenceSourceType.CHAT,
            source_ref=extracted.source_ref,
            current_milestone=(
                milestone_value
                if isinstance(milestone_value, Milestone)
                else Milestone(milestone_value)
            ),
            current_status=(
                status_value
                if isinstance(status_value, ProjectStatus)
                else ProjectStatus(status_value)
            ),
            current_ball_with=(
                ball_value
                if isinstance(ball_value, BallWith)
                else BallWith(ball_value)
            ),
            current_next_action=next_action,
            project_identity_resolved=True,
            facts=tuple(extracted.extracted_facts),
        ), policy=load_milestone_contract_policy())
    except (TypeError, ValueError):
        return None

def priority_criterion(assessment: ProjectStateAssessment):
    """Return first contradiction, otherwise first missing criterion."""
    return next((c for c in assessment.milestone_criteria
                 if c.result == CriterionResult.CONTRADICTED), None) or next(
        (c for c in assessment.milestone_criteria if c.result == CriterionResult.MISSING), None)


def citation_refs(assessment: ProjectStateAssessment) -> list[str]:
    """Return unique real evidence references used by deterministic evaluation."""
    refs = [citation.source_ref for criterion in assessment.milestone_criteria
            for citation in criterion.citations]
    return list(dict.fromkeys(refs))
