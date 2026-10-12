"""Compose pure milestone, status, ownership, and next-action assessment."""

from __future__ import annotations

from dataclasses import dataclass

from src.milestone.contracts import MilestoneContractPolicy
from src.models.enums import BallWith, Milestone, ProjectStatus
from src.models.progress_assessment import (
    AssessmentCode,
    EvidenceLevel,
    AssessmentDecision,
    ConfidenceVector,
    CriterionResult,
    EvidenceSourceType,
    ExtractedProgressFact,
    PctScopeException,
    ProjectStateAssessment,
)
from src.project_state.milestone_evaluator import evaluate_milestone_candidate
from src.project_state.ownership_evaluator import evaluate_ownership_and_next_action
from src.project_state.status_evaluator import evaluate_project_status


@dataclass(frozen=True)
class ProjectStateInput:
    project_id: str | None
    source_type: EvidenceSourceType
    source_ref: str
    current_milestone: Milestone | None
    current_status: ProjectStatus
    current_ball_with: BallWith
    current_next_action: str | None
    project_identity_resolved: bool
    facts: tuple[ExtractedProgressFact, ...]
    pct_scope_exceptions: tuple[PctScopeException, ...] = ()


def _confidence(
    request: ProjectStateInput,
    *,
    criteria,
) -> ConfidenceVector:
    fact_confidences = [fact.extraction_confidence for fact in request.facts]
    extraction = min(fact_confidences) if fact_confidences else 0.0
    citations = [c for fact in request.facts for c in fact.citations]
    source_reliability = 0.0
    if citations:
        rank = min(int(c.evidence_level.value[1]) for c in citations)
        source_reliability = max(0.0, 1.0 - ((rank - 1) * 0.2))
    temporal_certainty = (
        sum(1 for citation in citations if citation.event_at is not None) / len(citations)
        if citations else 0.0
    )
    applicable = [c for c in criteria if c.result != CriterionResult.NOT_APPLICABLE]
    met = sum(1 for c in applicable if c.result == CriterionResult.MET)
    contradicted = sum(1 for c in applicable if c.result == CriterionResult.CONTRADICTED)
    total = len(applicable)
    return ConfidenceVector(
        extraction=extraction,
        project_identity=1.0 if request.project_identity_resolved else 0.0,
        source_reliability=source_reliability,
        temporal_certainty=temporal_certainty,
        criteria_coverage=met / total if total else 0.0,
        contradiction_score=contradicted / total if total else 0.0,
    )


def assess_project_state(
    request: ProjectStateInput,
    *,
    policy: MilestoneContractPolicy,
) -> ProjectStateAssessment:
    milestone = evaluate_milestone_candidate(
        current_milestone=request.current_milestone,
        facts=request.facts,
        project_identity_resolved=request.project_identity_resolved,
        policy=policy,
        pct_scope_exceptions=request.pct_scope_exceptions,
    )
    status = evaluate_project_status(
        current_status=request.current_status,
        facts=request.facts,
        policy=policy,
    )
    first_missing = next(
        (
            criterion
            for criterion in milestone.criteria
            if criterion.result != CriterionResult.MET
        ),
        None,
    )
    later_stage_lag = (
        AssessmentCode.INVALID_STATE_TRANSITION in milestone.assessment.codes
    )
    follow_up = (
        None
        if later_stage_lag
        else (
            f"Obtain eligible evidence for {first_missing.criterion_id}."
            if first_missing else None
        )
    )
    from src.project_state.evidence_policy import fact_has_eligible_support
    allow_policy_follow_up = (not later_stage_lag) and any(
        fact_has_eligible_support(
            fact,
            minimum_level=EvidenceLevel.L4_TRUSTED_PARTY_WRITTEN,
            policy=policy,
        )[0]
        for fact in request.facts
    )
    ball, next_action = evaluate_ownership_and_next_action(
        current_ball_with=request.current_ball_with,
        current_next_action=request.current_next_action,
        facts=request.facts,
        policy=policy,
        policy_follow_up=follow_up,
        allow_policy_follow_up=allow_policy_follow_up,
    )
    codes = []
    if later_stage_lag:
        codes.append(AssessmentCode.INVALID_STATE_TRANSITION)
    if not request.project_identity_resolved:
        codes.append(AssessmentCode.AMBIGUOUS_PROJECT_IDENTITY)
        status = status.model_copy(update={
            "candidate_value": None,
            "decision": AssessmentDecision.CLARIFICATION_REQUIRED,
            "codes": [AssessmentCode.AMBIGUOUS_PROJECT_IDENTITY],
        })
        ball = ball.model_copy(update={
            "candidate_value": None,
            "decision": AssessmentDecision.CLARIFICATION_REQUIRED,
            "codes": [AssessmentCode.AMBIGUOUS_PROJECT_IDENTITY],
        })
        next_action = next_action.model_copy(update={
            "candidate_value": None,
            "decision": AssessmentDecision.CLARIFICATION_REQUIRED,
            "codes": [AssessmentCode.AMBIGUOUS_PROJECT_IDENTITY],
        })

    return ProjectStateAssessment(
        project_id=request.project_id if request.project_identity_resolved else None,
        source_type=request.source_type,
        source_ref=request.source_ref,
        extracted_facts=list(request.facts),
        milestone_criteria=list(milestone.criteria),
        milestone_assessment=milestone.assessment,
        status_assessment=status,
        ball_with_assessment=ball,
        next_action_assessment=next_action,
        contradictions=list(milestone.contradictions),
        confidence=_confidence(request, criteria=milestone.criteria),
        policy_version=policy.policy_version,
        assessment_codes=codes,
    )


def proposed_field_subset(assessment: ProjectStateAssessment) -> dict[str, str]:
    if AssessmentCode.INVALID_STATE_TRANSITION in assessment.assessment_codes:
        return {}
    fields: dict[str, str] = {}
    for item in (
        assessment.milestone_assessment,
        assessment.status_assessment,
        assessment.ball_with_assessment,
        assessment.next_action_assessment,
    ):
        if (
            item is not None
            and item.candidate_value is not None
            and item.decision in {
                AssessmentDecision.ACCEPT_CANDIDATE,
                AssessmentDecision.NEEDS_REVIEW,
            }
        ):
            fields[item.field.value] = item.candidate_value
    return fields
