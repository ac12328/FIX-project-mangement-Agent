"""Evidence-grounded project-state filtering for inbound email proposals."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Sequence

from src.email.email_envelope import build_attachment_context
from src.milestone.contracts import load_milestone_contract_policy
from src.models.enums import Milestone
from src.models.progress_assessment import (
    AssessmentCode,
    EvidenceCitation,
    EvidenceLevel,
    EvidenceSourceType,
    ExtractedProgressFact,
    NextActionProvenance,
    ProjectStateAssessment,
)
from src.project_state.evaluator import (
    ProjectStateInput,
    assess_project_state,
    proposed_field_subset,
)
from src.repositories.project_repo import get_project
from src.project_state.observability import project_state_metrics

logger = logging.getLogger(__name__)

_STATE_FIELDS = frozenset({
    "current_milestone",
    "project_status",
    "ball_with",
    "next_action",
})


@dataclass(frozen=True)
class EmailAssessmentResult:
    proposed_fields: dict[str, Any]
    assessment: ProjectStateAssessment | None
    milestone_validated: bool = False
    failure_code: str | None = None


def _citation_is_bounded(
    citation: EvidenceCitation,
    *,
    body: str,
    attachment_text: str,
) -> bool:
    from src.project_state.evidence_policy import citation_is_source_bounded

    return citation_is_source_bounded(
        citation,
        body=body,
        attachment_text=attachment_text,
    )


def _bounded_facts(
    facts: Sequence[ExtractedProgressFact],
    *,
    body: str,
    attachment_text: str,
) -> tuple[ExtractedProgressFact, ...]:
    bounded: list[ExtractedProgressFact] = []
    for fact in facts:
        # Only the deterministic transcript validator may emit this evidence.
        if fact.predicate == "validated_bidirectional_fix_session_log":
            continue
        citations = [
            citation
            for citation in fact.citations
            if _citation_is_bounded(
                citation,
                body=body,
                attachment_text=attachment_text,
            )
        ]
        bounded.append(fact.model_copy(update={"citations": citations}))
    return tuple(bounded)


def validate_assessment_for_review(
    assessment: dict[str, Any] | ProjectStateAssessment | None,
    *,
    project_id: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
    candidate_value: str | None = None,
) -> bool:
    """Validate milestone assessment identity, source, candidate, and decision."""
    from config import defaults
    from src.models.progress_assessment import AssessmentDecision

    try:
        parsed = (
            assessment if isinstance(assessment, ProjectStateAssessment)
            else ProjectStateAssessment.model_validate(assessment)
        )
    except Exception:
        return False
    milestone = parsed.milestone_assessment
    if parsed.policy_version != defaults.PROJECT_STATE_CONTRACT_VERSION or milestone is None:
        return False
    if project_id is not None and parsed.project_id != project_id:
        return False
    if source_type is not None and parsed.source_type.value != source_type:
        return False
    if source_ref is not None and parsed.source_ref != source_ref:
        return False
    if candidate_value is not None and milestone.candidate_value != candidate_value:
        return False
    return milestone.decision in {
        AssessmentDecision.ACCEPT_CANDIDATE,
        AssessmentDecision.NEEDS_REVIEW,
    }


def milestone_review_allowed(
    proposed_fields: dict[str, Any],
    assessment: dict[str, Any] | ProjectStateAssessment | None,
    *,
    legacy: bool,
    project_id: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
) -> bool:
    """Require a valid assessment only for newly generated milestone reviews."""
    from config import defaults

    if "current_milestone" not in proposed_fields:
        return True
    if legacy or not defaults.PROJECT_STATE_REQUIRE_ASSESSMENT_FOR_MILESTONE_REVIEW:
        return True
    return validate_assessment_for_review(
        assessment,
        project_id=project_id,
        source_type=source_type,
        source_ref=source_ref,
        candidate_value=proposed_fields.get("current_milestone"),
    )


def _record_assessment_metrics(assessment: ProjectStateAssessment) -> None:
    for item in (
        assessment.milestone_assessment,
        assessment.status_assessment,
        assessment.ball_with_assessment,
        assessment.next_action_assessment,
    ):
        if item is None:
            continue
        if item.candidate_value:
            project_state_metrics.record_candidate(
                item.field.value, item.current_value, item.candidate_value
            )
        elif item.codes:
            for code in item.codes:
                project_state_metrics.record_abstention(code.value)
    for criterion in assessment.milestone_criteria:
        if criterion.result.value == "missing":
            project_state_metrics.record_missing_criterion(criterion.criterion_id)
        elif criterion.result.value == "contradicted":
            project_state_metrics.record_contradiction("current_milestone")


def apply_email_project_state_policy(
    conn,
    *,
    project_id: str,
    proposed_fields: dict[str, Any] | None,
    assessment_detail: dict[str, Any] | None,
    source_ref: str,
    body: str,
    attachments: Sequence[Any] | None = None,
    force_evidence_grounded: bool = False,
    sender_email: str | None = None,
    received_at: str | datetime | None = None,
) -> EmailAssessmentResult:
    """Replace LLM state candidates with deterministic evidence-grounded results.

    Legacy payloads remain usable for non-state fields, but cannot establish any
    project-state field when assessment is missing or invalid. Structured facts are re-evaluated against current DB truth and the
    versioned operational contract; model-supplied field decisions are ignored.
    """
    from config import defaults

    fields = dict(proposed_fields or {})
    legacy_candidate = fields.get("current_milestone")
    if not force_evidence_grounded and not defaults.PROJECT_STATE_EVIDENCE_GROUNDED_ENABLED:
        return EmailAssessmentResult(fields, None)
    if not assessment_detail:
        for field in _STATE_FIELDS:
            fields.pop(field, None)
        if defaults.PROJECT_STATE_SHADOW_LEGACY_INFERENCE and legacy_candidate:
            project_state_metrics.record_disagreement(legacy_candidate, None)
        return EmailAssessmentResult(fields, None, failure_code="ASSESSMENT_MISSING")

    try:
        extracted = ProjectStateAssessment.model_validate(assessment_detail)
        if extracted.project_id != project_id or extracted.source_ref != source_ref:
            raise ValueError("assessment identity/source mismatch")
        project = get_project(conn, project_id)
        if project is None:
            raise ValueError("project not found")

        attachment_text = build_attachment_context(list(attachments or ()))
        from src.email.source_authority import resolve_source_authority, bound_pct_modality
        from src.email.body_cleaner import clean_email_body
        stored_source = conn.execute(
            "SELECT sender_email,received_at,project_id FROM processed_emails WHERE email_id=?",
            (source_ref,),
        ).fetchone()
        if stored_source and stored_source["project_id"] not in {None, project_id}:
            raise ValueError("stored email project mismatch")
        envelope_sender = sender_email or (stored_source["sender_email"] if stored_source else None)
        if sender_email and stored_source and stored_source["sender_email"]:
            from src.email.source_authority import normalized_sender
            if normalized_sender(sender_email) != normalized_sender(stored_source["sender_email"]):
                raise ValueError("stored email sender mismatch")
        source_time = (stored_source["received_at"] if stored_source else None) or received_at
        try:
            event_at = source_time if isinstance(source_time, datetime) else datetime.fromisoformat(source_time.replace("Z", "+00:00"))
            if event_at.tzinfo is None:
                event_at = event_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, AttributeError):
            event_at = None
        authority = resolve_source_authority(conn, project_id=project_id, sender_email=envelope_sender)
        # The model may classify content, but cannot assign its own identity or
        # evidence rank. Old quoted material is not the newest authored email.
        bound_input = [fact.model_copy(update={"citations": [
            citation.model_copy(update={"actor": authority.actor,
                "evidence_level": (
                    authority.evidence_level if citation.source_type == EvidenceSourceType.EMAIL
                    else EvidenceLevel.L4_TRUSTED_PARTY_WRITTEN
                    if authority.evidence_level != EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT
                    else EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT
                ), "event_at": event_at})
            for citation in fact.citations
            if (citation.source_type == EvidenceSourceType.EMAIL and citation.source_ref == source_ref)
            or (citation.source_type == EvidenceSourceType.ATTACHMENT
                and citation.source_ref.startswith(f"attachment:{source_ref}:"))
        ]}) for fact in extracted.extracted_facts]
        authored_body = clean_email_body(body)
        bound_input = [fact.model_copy(update={"citations": [
            bound_pct_modality(fact.predicate, citation, authored_body)
            if citation.source_type == EvidenceSourceType.EMAIL else citation
            for citation in fact.citations
        ]}) for fact in bound_input]
        facts = _bounded_facts(
            bound_input,
            body=clean_email_body(body),
            attachment_text=attachment_text,
        )
        request = ProjectStateInput(
            project_id=project_id,
            source_type=EvidenceSourceType.EMAIL,
            source_ref=source_ref,
            current_milestone=(
                project.current_milestone
                if isinstance(project.current_milestone, Milestone)
                else Milestone(project.current_milestone)
            ),
            current_status=project.project_status,
            current_ball_with=project.ball_with,
            current_next_action=project.next_action,
            project_identity_resolved=True,
            facts=facts,
        )
        policy = load_milestone_contract_policy()
        if policy.policy_version != defaults.PROJECT_STATE_CONTRACT_VERSION:
            raise ValueError("configured contract version mismatch")
        assessment = assess_project_state(request, policy=policy)
    except Exception as exc:
        event = (
            "contract_failure"
            if type(exc).__name__.startswith("MilestoneContract")
            or "contract version" in str(exc)
            else "assessment_parse_failure"
        )
        project_state_metrics.increment(event)
        logger.warning(
            "Email project-state assessment failed closed for %s (%s)",
            project_id,
            type(exc).__name__,
        )
        for field in _STATE_FIELDS:
            fields.pop(field, None)
        if defaults.PROJECT_STATE_SHADOW_LEGACY_INFERENCE and legacy_candidate:
            project_state_metrics.record_disagreement(legacy_candidate, None)
        failure_code = (
            "CONTRACT_FAILURE" if event == "contract_failure"
            else "ASSESSMENT_PARSE_FAILURE"
        )
        return EmailAssessmentResult(fields, None, failure_code=failure_code)

    _record_assessment_metrics(assessment)
    deterministic = proposed_field_subset(assessment)
    next_action = assessment.next_action_assessment
    if (
        next_action is not None
        and next_action.provenance == NextActionProvenance.POLICY_DERIVED_FOLLOW_UP
    ):
        deterministic.pop("next_action", None)
    if AssessmentCode.INVALID_STATE_TRANSITION in assessment.assessment_codes:
        deterministic = {}
    for field in _STATE_FIELDS:
        fields.pop(field, None)
    fields.update(deterministic)
    milestone_validated = "current_milestone" in deterministic
    new_candidate = deterministic.get("current_milestone")
    if (
        defaults.PROJECT_STATE_SHADOW_LEGACY_INFERENCE
        and legacy_candidate != new_candidate
    ):
        project_state_metrics.record_disagreement(legacy_candidate, new_candidate)
    return EmailAssessmentResult(fields, assessment, milestone_validated)
