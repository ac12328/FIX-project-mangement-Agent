"""Preserve source-bound deterministic ownership through legacy postprocessing.

This is not sender authentication or permission to trust model field assessments.
Callers supply the system-recomputed assessment and current source scope.
"""
from src.models.progress_assessment import (
    AssessmentDecision, ProjectStateAssessment, NextActionProvenance,
)
from src.milestone.contracts import MilestoneContractLoadError, load_milestone_contract_policy
from src.project_state.evidence_policy import citation_is_source_bounded


def evaluated_owner(assessment, *, project_id, source_ref, body, proposed_fields):
    if assessment is None or not project_id or not source_ref:
        return None
    try:
        parsed = assessment if isinstance(assessment, ProjectStateAssessment) else ProjectStateAssessment.model_validate(assessment)
        if parsed.project_id != project_id or parsed.source_ref != source_ref or parsed.source_type.value != "email":
            return None
        if parsed.policy_version != load_milestone_contract_policy().policy_version:
            return None
        ball, action = parsed.ball_with_assessment, parsed.next_action_assessment
        if ball is None or action is None:
            return None
        if ball.decision != AssessmentDecision.ACCEPT_CANDIDATE or action.decision != AssessmentDecision.ACCEPT_CANDIDATE:
            return None
        if action.provenance != NextActionProvenance.EXPLICIT_COMMITMENT:
            return None
        owner = ball.candidate_value
        if owner not in {"Internal", "External", "Shared"} or action.accountable_actor != owner:
            return None
        if proposed_fields.get("ball_with") != owner or not action.candidate_value or proposed_fields.get("next_action") != action.candidate_value:
            return None
        if not ball.citations or not action.citations:
            return None
        for citation in [*ball.citations, *action.citations]:
            modality_ok = citation.modality.value == "confirmed" or (
                citation.modality.value == "planned"
                and "next_action_commitment" in action.source_fact_predicates
            )
            if citation.source_ref != source_ref or citation.polarity.value != "positive" or not modality_ok:
                return None
            if not citation_is_source_bounded(citation, body=body or "", attachment_text=""):
                return None
        return owner
    except (ValueError, TypeError, MilestoneContractLoadError):
        return None
