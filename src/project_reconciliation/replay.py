"""Policy-governed chronological replay of frozen email evidence."""

from __future__ import annotations

from typing import Any

from src.milestone.contracts import load_milestone_contract_policy
from src.models.enums import Milestone, ProjectStatus, RiskLevel
from src.models.progress_assessment import (
    AssessmentDecision,
    ConfidenceVector,
    FieldAssessment,
    ProjectStateAssessment,
    ProjectStateField,
)
from src.project_reconciliation.contracts import (
    EvidenceCitation as ReconciliationCitation,
    FrozenEvidenceBundle,
    ReconciliationProposal,
)
from src.project_reconciliation.facts import extract_policy_facts
from src.project_reconciliation.progress_facts import CANONICAL_PROGRESS_PREDICATES
from src.project_state.milestone_evaluator import evaluate_milestone_candidate


_MILESTONES = list(Milestone)
REVIEWABLE_RECON_FIELDS = frozenset({
    "current_milestone",
    "project_status",
    "ball_with",
    "next_action",
    "target_go_live",
    "market",
})


def reviewable_recon_fields(fields: dict[str, Any] | None) -> dict[str, Any]:
    """Fields a PM can decide. Notes stamps are bookkeeping, not a review."""
    return {
        key: value
        for key, value in (fields or {}).items()
        if key in REVIEWABLE_RECON_FIELDS
    }
_NEXT_ACTION = {
    Milestone.M1_NETWORK_SETUP.value: "Confirm network connectivity and stable UAT FIX logon.",
    Milestone.M2_UAT_SESSION_CONFIGURATION.value: "Confirm the issued session can log on reliably in UAT.",
    Milestone.M3_UAT_ENVIRONMENT_ACCESS.value: "Validate basic UAT order flow and cancel handling.",
    Milestone.M4_APPLICATION_TESTING.value: (
        "Verify stable UAT logon/order flow and begin conformance testing."
    ),
    Milestone.M5_CONFORMANCE_TEST_CASES.value: (
        "Complete the conformance cases and collect the test report."
    ),
    Milestone.M6_TEST_REPORT_AND_PRODUCTION_PREPARATION.value: (
        "Complete Production Preparation configuration and production readiness checks."
    ),
    Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY.value: (
        "Complete log review/PCT and confirm the go-live date."
    ),
    Milestone.M8_GO_LIVE.value: (
        "Monitor production stability and complete the closing checklist."
    ),
}


def _milestone(value: Any) -> Milestone | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, Milestone) else Milestone(str(value))
    except ValueError:
        return None


def _unique_policy_citations(step_results) -> list:
    seen: set[tuple[str, str]] = set()
    citations = []
    for result in step_results:
        for citation in result.assessment.citations:
            key = (citation.source_ref, citation.quote)
            if key in seen:
                continue
            seen.add(key)
            citations.append(citation)
    return citations


def _aggregate_assessment(
    *,
    bundle: FrozenEvidenceBundle,
    initial: Milestone | None,
    target: Milestone,
    facts,
    step_results,
    policy_version: str,
) -> ProjectStateAssessment:
    citations = _unique_policy_citations(step_results)
    criteria = [criterion for result in step_results for criterion in result.criteria]
    reasons = [
        f"Evidence met {', '.join(criterion.criterion_id for criterion in result.criteria)} "
        f"and supports {result.assessment.candidate_value}; intermediate history was not inferred."
        for result in step_results
    ]
    confidence = min((fact.extraction_confidence for fact in facts), default=0.0)
    return ProjectStateAssessment(
        project_id=bundle.project_id,
        source_type="email",
        source_ref=f"email-bundle:{bundle.bundle_hash}",
        extracted_facts=facts,
        milestone_criteria=criteria,
        milestone_assessment=FieldAssessment(
            field=ProjectStateField.CURRENT_MILESTONE,
            current_value=initial.value if initial else None,
            candidate_value=target.value,
            decision=AssessmentDecision.NEEDS_REVIEW,
            reasons=reasons or ["Historical evidence supports milestone catch-up."],
            risk_level=RiskLevel.HIGH,
            citations=citations,
        ),
        confidence=ConfidenceVector(
            extraction=confidence,
            project_identity=1.0,
            source_reliability=1.0 if citations else 0.0,
            temporal_certainty=(
                sum(citation.event_at is not None for citation in citations) / len(citations)
                if citations else 0.0
            ),
            criteria_coverage=1.0 if criteria else 0.0,
            contradiction_score=0.0,
        ),
        policy_version=policy_version,
    )


def build_proposal(
    bundle: FrozenEvidenceBundle,
    *,
    current_project: dict | None,
    source_authorities=None,
    delivery_confirmations=None,
) -> ReconciliationProposal:
    """Replay evidence through adjacent contract transitions without regression."""
    policy = load_milestone_contract_policy()
    project_exists = current_project is not None
    current_milestone = _milestone(
        (current_project or {}).get("current_milestone")
    )
    initial_for_replay = current_milestone or Milestone.M1_NETWORK_SETUP
    virtual = initial_for_replay
    facts = extract_policy_facts(bundle, source_authorities=source_authorities,
        delivery_confirmations=delivery_confirmations)
    step_results = []
    canonical_progress = any(fact.predicate in CANONICAL_PROGRESS_PREDICATES for fact in facts)

    while virtual != Milestone.M8_GO_LIVE:
        result = evaluate_milestone_candidate(
            current_milestone=virtual,
            facts=facts,
            project_identity_resolved=True,
            policy=policy,
        )
        candidate_value = result.assessment.candidate_value
        if result.assessment.decision not in {
            AssessmentDecision.ACCEPT_CANDIDATE,
            AssessmentDecision.NEEDS_REVIEW,
        } or not candidate_value:
            break
        candidate = _milestone(candidate_value)
        if candidate is None or candidate.index <= virtual.index:
            break
        step_results.append(result)
        virtual = candidate

    target = virtual
    current_value = current_milestone.value if current_milestone else None
    fields: dict[str, Any] = {}
    milestone_advanced = target.value != current_value
    if milestone_advanced:
        fields["current_milestone"] = target.value

    current_status = (current_project or {}).get("project_status")
    terminal = {status.value for status in ProjectStatus.terminal_statuses()}
    if bundle.records and current_status and current_status not in terminal:
        if current_status not in {
            ProjectStatus.ACTIVE.value,
            ProjectStatus.BLOCKED.value,
            ProjectStatus.WAITING.value,
        }:
            fields["project_status"] = ProjectStatus.ACTIVE.value

    is_terminal = current_status in terminal
    action = _NEXT_ACTION.get(target.value)
    # Stage templates are only for a newly entered milestone. A no-advance
    # replay must not clobber a more recent operator/email next_action.
    # Historical catch-up needs an independently grounded action, not stage busywork.
    if (
        milestone_advanced
        and not canonical_progress
        and bundle.records
        and not is_terminal
        and action
        and (current_project or {}).get("next_action") != action
    ):
        fields["next_action"] = action

    current_notes = str((current_project or {}).get("notes") or "")
    marker = f"[email-reconciliation:{bundle.bundle_hash[:12]}]"
    if bundle.records and not is_terminal and marker not in current_notes:
        count = len(bundle.records)
        label = "message" if count == 1 else "messages"
        summary = f"{marker} Email history reconciled ({count} {label})."
        fields["notes"] = f"{current_notes.rstrip()}\n{summary}".strip()

    policy_citations = _unique_policy_citations(step_results)
    citations = [
        ReconciliationCitation(
            source_ref=citation.source_ref,
            quote=citation.quote,
            event_at=citation.event_at.isoformat() if citation.event_at else "",
            reason="Accepted by the milestone contract evaluator.",
        )
        for citation in policy_citations
    ]
    assessment = None
    if target.index > initial_for_replay.index:
        assessment = _aggregate_assessment(
            bundle=bundle,
            initial=current_milestone,
            target=target,
            facts=facts,
            step_results=step_results,
            policy_version=policy.policy_version,
        ).model_dump(mode="json")

    confirmation_refs = sorted({
        fact.subject for fact in facts
        if fact.subject and fact.subject.startswith("delivery-confirmation:")
        and any(citation in policy_citations for citation in fact.citations)
    })
    if assessment and confirmation_refs:
        assessment["milestone_assessment"]["reasons"].extend(
            f"Source authority/project identity confirmed separately by operator; manual-note reference {ref}. Original email retained."
            for ref in confirmation_refs
        )

    replay_steps = [
        {
            "from_milestone": result.assessment.current_value,
            "to_milestone": result.assessment.candidate_value,
            "decision": result.assessment.decision.value,
            "criteria": [criterion.model_dump(mode="json") for criterion in result.criteria],
        }
        for result in step_results
    ]
    rationale = [
        f"Evidence supports correction from {step['from_milestone']} to {step['to_milestone']}; "
        "no intermediate completion event was created."
        for step in replay_steps
    ]
    if not step_results and bundle.records:
        rationale.append("Evidence was preserved, but no additional milestone exit gate was satisfied.")

    if not bundle.records or is_terminal:
        fields = {}

    confidence = min((fact.extraction_confidence for fact in facts), default=0.0)
    return ReconciliationProposal(
        project_id=bundle.project_id,
        project_exists=project_exists,
        base_version=(current_project or {}).get("updated_at"),
        fields=fields,
        inferred_milestone=target.value,
        bundle_hash=bundle.bundle_hash,
        evidence_refs=[record.source_ref for record in bundle.records] + confirmation_refs,
        citations=citations,
        rationale=rationale,
        confidence=confidence,
        assessment=assessment,
        replay_steps=replay_steps,
    )
