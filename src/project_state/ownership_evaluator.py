"""Independent ball-with and next-action assessment."""

from __future__ import annotations

from collections.abc import Sequence

from src.milestone.contracts import MilestoneContractPolicy
from src.models.enums import BallWith, RiskLevel
from src.models.progress_assessment import (
    AssessmentCode,
    AssessmentDecision,
    EvidenceLevel,
    EvidenceModality,
    ExtractedProgressFact,
    FieldAssessment,
    NextActionAssessment,
    NextActionProvenance,
    ProjectStateField,
)


_ACTOR_MAP = {
    "internal": BallWith.INTERNAL,
    "connectivity_acceptance": BallWith.INTERNAL,
    "demo_organization": BallWith.INTERNAL,
    "external": BallWith.EXTERNAL,
    "client": BallWith.EXTERNAL,
    "counterparty": BallWith.EXTERNAL,
    "shared": BallWith.SHARED,
}


def _actor_ball(subject: str | None) -> BallWith | None:
    if not subject:
        return None
    return _ACTOR_MAP.get(subject.strip().casefold())


def evaluate_ownership_and_next_action(
    *,
    current_ball_with: BallWith,
    current_next_action: str | None,
    facts: Sequence[ExtractedProgressFact],
    policy: MilestoneContractPolicy,
    policy_follow_up: str | None = None,
    allow_policy_follow_up: bool = False,
) -> tuple[FieldAssessment, NextActionAssessment]:
    from src.project_state.evidence_policy import fact_has_eligible_support

    obligations = []
    eligible_by_id = {}
    for fact in facts:
        if (
            fact.predicate not in {"explicit_obligation", "next_action_commitment"}
            or _actor_ball(fact.subject) is None
            or not isinstance(fact.value, str)
            or not fact.value.strip()
        ):
            continue
        # A confirmed promise concerns future work. Its existence may establish
        # ownership without proving the work or a milestone has completed.
        eligible_fact = fact
        if fact.predicate == "next_action_commitment":
            eligible_fact = fact.model_copy(update={"citations": [
                c.model_copy(update={"modality": EvidenceModality.CONFIRMED})
                if c.modality == EvidenceModality.PLANNED else c
                for c in fact.citations
            ]})
        eligible, _ = fact_has_eligible_support(
            eligible_fact,
            minimum_level=EvidenceLevel.L4_TRUSTED_PARTY_WRITTEN,
            policy=policy,
        )
        # Preserve original modality in the audit evidence.
        citations = [
            original for original, effective in zip(fact.citations, eligible_fact.citations)
            if effective in eligible
        ]
        if citations:
            obligations.append(fact)
            eligible_by_id[id(fact)] = citations
    obligations = sorted(
        obligations,
        key=lambda fact: (
            str(fact.value or ""),
            fact.subject or "",
            fact.predicate,
        ),
    )
    balls = {_actor_ball(fact.subject) for fact in obligations}
    actions = {str(fact.value).strip() for fact in obligations if fact.value is not None}
    owners_by_action = {}
    for obligation in obligations:
        key = str(obligation.value or "").strip().casefold()
        owners_by_action.setdefault(key, set()).add(_actor_ball(obligation.subject))
    # Different tasks are not contradictions. The same task assigned conflicting
    # sole owners still needs clarification unless represented as explicitly shared.
    if any(len(owners) > 1 for owners in owners_by_action.values()):
        ball = FieldAssessment(
            field=ProjectStateField.BALL_WITH,
            current_value=current_ball_with.value,
            decision=AssessmentDecision.CLARIFICATION_REQUIRED,
            codes=[AssessmentCode.CONTRADICTORY_EVIDENCE],
            risk_level=RiskLevel.MEDIUM,
            citations=[c for fact in obligations for c in eligible_by_id[id(fact)]],
        )
        next_action = NextActionAssessment(
            current_value=current_next_action,
            decision=AssessmentDecision.CLARIFICATION_REQUIRED,
            codes=[AssessmentCode.CONTRADICTORY_EVIDENCE],
            risk_level=RiskLevel.MEDIUM,
            provenance=NextActionProvenance.EXPLICIT_COMMITMENT,
            source_fact_predicates=[fact.predicate for fact in obligations],
        )
        return ball, next_action

    if obligations:
        fact = obligations[0]
        candidate_ball = BallWith.SHARED if len(balls) > 1 or BallWith.SHARED in balls else _actor_ball(fact.subject)
        if len(actions) <= 1:
            action = str(fact.value).strip() if fact.value is not None else ""
        elif candidate_ball == BallWith.SHARED:
            action = "; ".join(dict.fromkeys(
                f"{_actor_ball(item.subject).value}: {str(item.value).strip()}"
                for item in obligations if item.value is not None
            ))
        else:
            action = "; ".join(sorted(action for action in actions if action))
        citations = [c for item in obligations for c in eligible_by_id[id(item)]]
        ball = FieldAssessment(
            field=ProjectStateField.BALL_WITH,
            current_value=current_ball_with.value,
            candidate_value=(
                candidate_ball.value
                if candidate_ball and candidate_ball != current_ball_with
                else None
            ),
            decision=AssessmentDecision.ACCEPT_CANDIDATE,
            reasons=["The next accountable actor is explicitly identified."],
            risk_level=RiskLevel.LOW,
            citations=citations,
        )
        next_action = NextActionAssessment(
            current_value=current_next_action,
            candidate_value=action or None,
            decision=(
                AssessmentDecision.ACCEPT_CANDIDATE
                if action else AssessmentDecision.INSUFFICIENT_EVIDENCE
            ),
            reasons=["The source states an explicit next obligation."] if action else [],
            codes=[] if action else [AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE],
            risk_level=RiskLevel.LOW,
            citations=citations,
            provenance=NextActionProvenance.EXPLICIT_COMMITMENT,
            accountable_actor=candidate_ball.value if candidate_ball else None,
            source_fact_predicates=[fact.predicate for fact in obligations],
        )
        return ball, next_action

    ball = FieldAssessment(
        field=ProjectStateField.BALL_WITH,
        current_value=current_ball_with.value,
        decision=AssessmentDecision.INSUFFICIENT_EVIDENCE,
        codes=[AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE],
        risk_level=RiskLevel.LOW,
    )
    if policy_follow_up and allow_policy_follow_up:
        next_action = NextActionAssessment(
            current_value=current_next_action,
            candidate_value=policy_follow_up,
            decision=AssessmentDecision.ACCEPT_CANDIDATE,
            reasons=["Derived from the first missing milestone criterion."],
            risk_level=RiskLevel.LOW,
            provenance=NextActionProvenance.POLICY_DERIVED_FOLLOW_UP,
        )
    else:
        next_action = NextActionAssessment(
            current_value=current_next_action,
            decision=AssessmentDecision.INSUFFICIENT_EVIDENCE,
            codes=[AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE],
            risk_level=RiskLevel.LOW,
            provenance=NextActionProvenance.GENERATED_SUGGESTION,
        )
    return ball, next_action
