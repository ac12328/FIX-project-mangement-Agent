"""Pure milestone criteria and adjacent-transition evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.milestone.contracts import (
    ContractCriterion,
    MilestoneContract,
    MilestoneContractPolicy,
    get_milestone_contract,
)
from src.models.enums import Milestone, RiskLevel
from src.models.progress_assessment import (
    AssessmentCode,
    AssessmentDecision,
    CriterionEvaluation,
    CriterionResult,
    EvidenceCitation,
    EvidenceLevel,
    EvidenceModality,
    EvidencePolarity,
    ExtractedProgressFact,
    FieldAssessment,
    PctScopeException,
    ProjectStateField,
)
from src.project_state.evidence_policy import (
    fact_has_eligible_support,
    meets_minimum_evidence_level,
    negative_citations_for_facts,
    resolve_evidence_conflict,
)
from src.project_state.later_stage import detect_later_stage_lag, later_stage_reason


@dataclass(frozen=True)
class MilestoneEvaluation:
    criteria: tuple[CriterionEvaluation, ...]
    candidate: Milestone | None
    assessment: FieldAssessment
    contradictions: tuple[EvidenceCitation, ...]


def _valid_pct_exception(
    exception: PctScopeException,
    *,
    policy: MilestoneContractPolicy,
) -> bool:
    pct = policy.pct_scope_policy
    allowed_steps = set(pct.standard_connectivity_steps + pct.standard_order_flow_steps)
    authorities = {
        authority.casefold()
        for authority in pct.not_applicable_policy.approval_authorities
    }
    citation = exception.citation
    return (
        exception.step_id in allowed_steps
        and exception.approved_by.casefold() in authorities
        and citation.polarity == EvidencePolarity.POSITIVE
        and citation.modality == EvidenceModality.CONFIRMED
        and meets_minimum_evidence_level(
            citation,
            EvidenceLevel.L2_AUTHORITATIVE_SYSTEM,
            policy,
        )
        and citation.source_ref == exception.source_ref
        and citation.actor is not None
        and citation.actor.casefold() == exception.approved_by.casefold()
    )


def _pct_criterion_met(
    criterion: ContractCriterion,
    facts: Sequence[ExtractedProgressFact],
    exceptions: Sequence[PctScopeException],
    policy: MilestoneContractPolicy,
) -> tuple[bool, list[EvidenceCitation], list[AssessmentCode]]:
    if criterion.id == "M7-EXIT-03":
        required_steps = policy.pct_scope_policy.standard_connectivity_steps
        predicate_by_step = {
            "primary": "pct_primary_connectivity_passed",
            "backup": "pct_backup_connectivity_passed",
            "failover": "pct_failover_validated",
            "failback": "pct_failback_validated",
        }
    elif criterion.id == "M7-EXIT-04":
        required_steps = policy.pct_scope_policy.standard_order_flow_steps
        predicate_by_step = {
            "test_order": "pct_test_order_sent",
            "expected_execution_report_or_reject": (
                "pct_expected_execution_report_or_reject_received"
            ),
        }
    else:
        return False, [], []

    citations: list[EvidenceCitation] = []
    codes: list[AssessmentCode] = []
    valid_exceptions = {
        exception.step_id: exception
        for exception in exceptions
        if _valid_pct_exception(exception, policy=policy)
    }
    all_pct_steps = set(
        policy.pct_scope_policy.standard_connectivity_steps
        + policy.pct_scope_policy.standard_order_flow_steps
    )
    if (
        policy.pct_scope_policy.not_applicable_policy.blanket_not_applicable_for_all_steps_forbidden
        and all_pct_steps
        and all_pct_steps <= set(valid_exceptions)
    ):
        return False, [], [AssessmentCode.NON_STANDARD_PATH_REVIEW_REQUIRED]
    minimum = criterion.minimum_evidence_level or EvidenceLevel.L1_OPERATOR_CONFIRMED
    for step in required_steps:
        if step in valid_exceptions:
            citations.append(valid_exceptions[step].citation)
            continue
        predicate = predicate_by_step[step]
        matching = [fact for fact in facts if fact.predicate == predicate]
        supported = []
        for fact in matching:
            eligible, fact_codes = fact_has_eligible_support(
                fact,
                minimum_level=minimum,
                policy=policy,
            )
            supported.extend(eligible)
            codes.extend(code for code in fact_codes if code not in codes)
        if not supported:
            if AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE not in codes:
                codes.append(AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE)
            return False, citations, codes
        citations.extend(supported)
    return True, citations, codes


def evaluate_exit_criteria(
    contract: MilestoneContract,
    facts: Sequence[ExtractedProgressFact],
    *,
    policy: MilestoneContractPolicy,
    pct_scope_exceptions: Sequence[PctScopeException] = (),
) -> tuple[CriterionEvaluation, ...]:
    # Contract list order is the authoritative missing-criterion priority order.
    # The first unresolved criterion is therefore the next operational gap.
    evaluations: list[CriterionEvaluation] = []
    for criterion in contract.required_exit_criteria:
        predicates = set(criterion.accepted_evidence_predicates)
        minimum = (
            criterion.minimum_evidence_level
            or EvidenceLevel.L1_OPERATOR_CONFIRMED
        )
        negatives = negative_citations_for_facts(
            facts,
            predicates,
            policy,
            minimum_level=minimum,
            authority_names=criterion.signoff_authorities,
            authority_required=bool(
                criterion.named_authority_required_for_signoff
            ),
        )

        if criterion.id in {"M7-EXIT-03", "M7-EXIT-04"}:
            met, citations, codes = _pct_criterion_met(
                criterion,
                facts,
                pct_scope_exceptions,
                policy,
            )
        else:
            citations = []
            codes = []
            for fact in facts:
                if fact.predicate not in predicates:
                    continue
                eligible, fact_codes = fact_has_eligible_support(
                    fact,
                    minimum_level=(
                        criterion.minimum_evidence_level
                        or EvidenceLevel.L1_OPERATOR_CONFIRMED
                    ),
                    policy=policy,
                    authority_names=criterion.signoff_authorities,
                    authority_required=bool(
                        criterion.named_authority_required_for_signoff
                    ),
                )
                citations.extend(eligible)
                codes.extend(code for code in fact_codes if code not in codes)
            met = bool(citations)

        resolution = resolve_evidence_conflict(citations, negatives, policy)
        if resolution in {"negative", "conflict"}:
            evaluations.append(CriterionEvaluation(
                criterion_id=criterion.id,
                result=CriterionResult.CONTRADICTED,
                citations=negatives,
                explanation=f"Negative evidence contradicts {criterion.description}",
                codes=[AssessmentCode.CONTRADICTORY_EVIDENCE],
            ))
            continue
        if (
            resolution == "positive"
            and criterion.id not in {"M7-EXIT-03", "M7-EXIT-04"}
        ):
            met = True

        if met:
            evaluations.append(CriterionEvaluation(
                criterion_id=criterion.id,
                result=CriterionResult.MET,
                citations=citations,
                explanation=criterion.description,
            ))
        else:
            if not codes:
                codes = [AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE]
            evaluations.append(CriterionEvaluation(
                criterion_id=criterion.id,
                result=CriterionResult.MISSING,
                explanation=f"Missing eligible evidence: {criterion.description}",
                codes=codes,
            ))
    return tuple(evaluations)


def evaluate_milestone_candidate(
    *,
    current_milestone: Milestone | None,
    facts: Sequence[ExtractedProgressFact],
    project_identity_resolved: bool,
    policy: MilestoneContractPolicy,
    pct_scope_exceptions: Sequence[PctScopeException] = (),
) -> MilestoneEvaluation:
    if not project_identity_resolved:
        assessment = FieldAssessment(
            field=ProjectStateField.CURRENT_MILESTONE,
            current_value=current_milestone.value if current_milestone else None,
            decision=AssessmentDecision.CLARIFICATION_REQUIRED,
            codes=[AssessmentCode.AMBIGUOUS_PROJECT_IDENTITY],
            risk_level=RiskLevel.HIGH,
        )
        return MilestoneEvaluation((), None, assessment, ())

    if current_milestone is None:
        onboarding = []
        for fact in facts:
            if fact.predicate != "onboarding_project_identified":
                continue
            eligible, _ = fact_has_eligible_support(
                fact,
                minimum_level=EvidenceLevel.L3_NAMED_AUTHORITY_WRITTEN,
                policy=policy,
            )
            if eligible:
                onboarding.append(fact)
        candidate = Milestone.M1_NETWORK_SETUP if onboarding else None
        assessment = FieldAssessment(
            field=ProjectStateField.CURRENT_MILESTONE,
            candidate_value=candidate.value if candidate else None,
            decision=(
                AssessmentDecision.NEEDS_REVIEW
                if candidate
                else AssessmentDecision.INSUFFICIENT_EVIDENCE
            ),
            reasons=["Onboarding project identity is confirmed."] if candidate else [],
            codes=[] if candidate else [AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE],
            risk_level=RiskLevel.MEDIUM,
            citations=[c for fact in onboarding for c in fact.citations],
        )
        return MilestoneEvaluation((), candidate, assessment, ())

    contract = next(item for item in policy.milestones if item.id == current_milestone)
    criteria = evaluate_exit_criteria(
        contract,
        facts,
        policy=policy,
        pct_scope_exceptions=pct_scope_exceptions,
    )
    contradictions = tuple(
        citation
        for criterion in criteria
        if criterion.result == CriterionResult.CONTRADICTED
        for citation in criterion.citations
    )
    all_met = bool(criteria) and all(
        criterion.result == CriterionResult.MET for criterion in criteria
    )
    next_values = contract.allowed_transitions.next
    candidate = next_values[0] if all_met and next_values else None
    # Confirmed current progress can establish a later M2-M7 entry gate without
    # fabricating intermediate history. Do not infer a jump to M8 from substeps.
    if not contradictions:
        current_index = next(i for i, item in enumerate(policy.milestones) if item.id == current_milestone)
        for later_contract in policy.milestones[current_index + 1:]:
            later_targets = later_contract.allowed_transitions.next
            if not later_targets or later_targets[0] == Milestone.M8_GO_LIVE:
                continue
            later_criteria = evaluate_exit_criteria(later_contract, facts, policy=policy,
                pct_scope_exceptions=pct_scope_exceptions)
            if later_criteria and all(item.result == CriterionResult.MET for item in later_criteria):
                candidate = later_targets[0]
                criteria = later_criteria
                contract = later_contract
    lag = detect_later_stage_lag(
        current_milestone=current_milestone,
        facts=facts,
        policy=policy,
    )
    if contradictions:
        decision = AssessmentDecision.CONTRADICTION
        codes = [AssessmentCode.CONTRADICTORY_EVIDENCE]
        reasons: list[str] = []
        risk = RiskLevel.HIGH
        citations = list(contradictions)
    # A later-stage hint without a qualifying gate remains a lag warning.
    # Valid current evidence may establish the higher M2-M7 candidate above,
    # but unresolved current-gate contradictions still fail closed.
    elif lag is not None and candidate is None:
        candidate = None
        decision = AssessmentDecision.CONTRADICTION
        codes = [AssessmentCode.INVALID_STATE_TRANSITION]
        reasons = [later_stage_reason(current_milestone, lag.implied_milestone)]
        contradictions = lag.citations
        risk = RiskLevel.HIGH
        citations = list(lag.citations)
    elif candidate:
        decision = AssessmentDecision.NEEDS_REVIEW
        codes = []
        reasons = [f"Confirmed evidence meets {contract.id.value} exit criteria for {candidate.value}."]
        risk = RiskLevel.MEDIUM
        citations = [
            citation
            for criterion in criteria
            if criterion.result == CriterionResult.MET
            for citation in criterion.citations
        ]
    else:
        decision = AssessmentDecision.INSUFFICIENT_EVIDENCE
        codes = list(dict.fromkeys(
            code for criterion in criteria for code in criterion.codes
        )) or [AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE]
        reasons = []
        risk = RiskLevel.MEDIUM
        citations = []

    assessment = FieldAssessment(
        field=ProjectStateField.CURRENT_MILESTONE,
        current_value=current_milestone.value,
        candidate_value=candidate.value if candidate else None,
        decision=decision,
        reasons=reasons,
        codes=codes,
        risk_level=risk,
        citations=citations,
    )
    return MilestoneEvaluation(criteria, candidate, assessment, contradictions)
