"""Independent deterministic project-status assessment."""

from __future__ import annotations

from collections.abc import Sequence

from src.milestone.contracts import MilestoneContractPolicy
from src.models.enums import ProjectStatus, RiskLevel
from src.models.progress_assessment import (
    AssessmentCode,
    AssessmentDecision,
    EvidenceLevel,
    ExtractedProgressFact,
    FieldAssessment,
    ProjectStateField,
)
from src.project_state.evidence_policy import fact_has_eligible_support


def _trusted_citations(
    facts: Sequence[ExtractedProgressFact],
    predicate: str,
    policy: MilestoneContractPolicy,
) -> list:
    citations = []
    for fact in facts:
        if fact.predicate != predicate:
            continue
        eligible, _ = fact_has_eligible_support(
            fact,
            minimum_level=EvidenceLevel.L4_TRUSTED_PARTY_WRITTEN,
            policy=policy,
        )
        citations.extend(eligible)
    return citations


def evaluate_project_status(
    *,
    current_status: ProjectStatus,
    facts: Sequence[ExtractedProgressFact],
    policy: MilestoneContractPolicy,
) -> FieldAssessment:
    if current_status in ProjectStatus.terminal_statuses():
        return FieldAssessment(
            field=ProjectStateField.PROJECT_STATUS,
            current_value=current_status.value,
            decision=AssessmentDecision.REJECT,
            reasons=["Terminal projects remain read-only unless explicitly reopened."],
            risk_level=RiskLevel.HIGH,
        )

    terminal_map = {
        "project_completed_confirmed": ProjectStatus.COMPLETED,
        "project_cancelled_confirmed": ProjectStatus.CANCELLED,
        "project_archived_confirmed": ProjectStatus.ARCHIVED,
    }
    terminal_matches = [
        (candidate, citations)
        for predicate, candidate in terminal_map.items()
        if (citations := _trusted_citations(facts, predicate, policy))
    ]
    terminal_values = {candidate for candidate, _ in terminal_matches}
    if len(terminal_values) > 1:
        return FieldAssessment(
            field=ProjectStateField.PROJECT_STATUS,
            current_value=current_status.value,
            decision=AssessmentDecision.CLARIFICATION_REQUIRED,
            codes=[AssessmentCode.CONTRADICTORY_EVIDENCE],
            risk_level=RiskLevel.HIGH,
            citations=[c for _, items in terminal_matches for c in items],
        )
    if terminal_matches:
        candidate, citations = terminal_matches[0]
        return FieldAssessment(
            field=ProjectStateField.PROJECT_STATUS,
            current_value=current_status.value,
            candidate_value=candidate.value,
            decision=AssessmentDecision.NEEDS_REVIEW,
            codes=[AssessmentCode.TERMINAL_STATUS_REVIEW_REQUIRED],
            risk_level=RiskLevel.HIGH,
            citations=citations,
        )

    blockers = _trusted_citations(facts, "blocking_issue_confirmed", policy)
    blockers += _trusted_citations(facts, "production_blocker_confirmed", policy)
    if blockers and current_status != ProjectStatus.BLOCKED:
        return FieldAssessment(
            field=ProjectStateField.PROJECT_STATUS,
            current_value=current_status.value,
            candidate_value=ProjectStatus.BLOCKED.value,
            decision=AssessmentDecision.NEEDS_REVIEW,
            reasons=["A confirmed blocking issue prevents progress."],
            risk_level=RiskLevel.HIGH,
            citations=blockers,
        )

    pending = _trusted_citations(facts, "pending_explicit_obligation", policy)
    if pending and current_status != ProjectStatus.WAITING:
        return FieldAssessment(
            field=ProjectStateField.PROJECT_STATUS,
            current_value=current_status.value,
            candidate_value=ProjectStatus.WAITING.value,
            decision=AssessmentDecision.ACCEPT_CANDIDATE,
            reasons=["A concrete accountable-party obligation is pending."],
            risk_level=RiskLevel.LOW,
            citations=pending,
        )

    active = _trusted_citations(facts, "work_in_progress_confirmed", policy)
    resolved = _trusted_citations(facts, "blocker_resolved_confirmed", policy)
    if (active or resolved) and current_status != ProjectStatus.ACTIVE:
        return FieldAssessment(
            field=ProjectStateField.PROJECT_STATUS,
            current_value=current_status.value,
            candidate_value=ProjectStatus.ACTIVE.value,
            decision=AssessmentDecision.ACCEPT_CANDIDATE,
            reasons=["Confirmed work is progressing without a current blocker."],
            risk_level=RiskLevel.LOW,
            citations=active or resolved,
        )

    return FieldAssessment(
        field=ProjectStateField.PROJECT_STATUS,
        current_value=current_status.value,
        decision=AssessmentDecision.INSUFFICIENT_EVIDENCE,
        codes=[AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE],
        risk_level=RiskLevel.LOW,
    )
