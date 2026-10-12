"""Pure evidence eligibility and conflict-ordering helpers."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime, timezone

from src.milestone.contracts import MilestoneContractPolicy
from src.models.progress_assessment import (
    AssessmentCode,
    EvidenceCitation,
    EvidenceLevel,
    EvidenceModality,
    EvidencePolarity,
    ExtractedProgressFact,
)


def evidence_rank(level: EvidenceLevel, policy: MilestoneContractPolicy) -> int:
    return policy.evidence_levels[level].rank


def meets_minimum_evidence_level(
    citation: EvidenceCitation,
    minimum: EvidenceLevel,
    policy: MilestoneContractPolicy,
) -> bool:
    return evidence_rank(citation.evidence_level, policy) <= evidence_rank(minimum, policy)


def citation_is_exit_eligible(
    citation: EvidenceCitation,
    *,
    minimum_level: EvidenceLevel,
    policy: MilestoneContractPolicy,
) -> tuple[bool, AssessmentCode | None]:
    if citation.polarity != EvidencePolarity.POSITIVE:
        return False, AssessmentCode.CONTRADICTORY_EVIDENCE
    if citation.modality == EvidenceModality.PLANNED:
        return False, AssessmentCode.FUTURE_INTENT_NOT_COMPLETION
    if citation.modality == EvidenceModality.CONDITIONAL:
        return False, AssessmentCode.CONDITIONAL_STATEMENT
    if citation.modality == EvidenceModality.IN_PROGRESS:
        return False, AssessmentCode.PARTIAL_COMPLETION
    if citation.modality != EvidenceModality.CONFIRMED:
        return False, AssessmentCode.INSUFFICIENT_EXIT_EVIDENCE
    if not meets_minimum_evidence_level(citation, minimum_level, policy):
        return False, AssessmentCode.UNAUTHORIZED_SOURCE
    return True, None


def fact_has_eligible_support(
    fact: ExtractedProgressFact,
    *,
    minimum_level: EvidenceLevel,
    policy: MilestoneContractPolicy,
    authority_names: Collection[str] = (),
    authority_required: bool = False,
) -> tuple[list[EvidenceCitation], list[AssessmentCode]]:
    authorities = {name.strip().casefold() for name in authority_names}
    eligible: list[EvidenceCitation] = []
    codes: list[AssessmentCode] = []
    for citation in fact.citations:
        ok, code = citation_is_exit_eligible(
            citation,
            minimum_level=minimum_level,
            policy=policy,
        )
        if not ok:
            if code and code not in codes:
                codes.append(code)
            continue
        if authority_required and (citation.actor or "").strip().casefold() not in authorities:
            if AssessmentCode.UNAUTHORIZED_SOURCE not in codes:
                codes.append(AssessmentCode.UNAUTHORIZED_SOURCE)
            continue
        eligible.append(citation)
    return choose_effective_citations(eligible, policy), codes


def choose_effective_citations(
    citations: Sequence[EvidenceCitation],
    policy: MilestoneContractPolicy,
) -> list[EvidenceCitation]:
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    def _timestamp(citation: EvidenceCitation) -> datetime:
        value = citation.event_at
        if value is None:
            return oldest
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    return sorted(
        citations,
        key=lambda citation: (
            -_timestamp(citation).timestamp(),
            evidence_rank(citation.evidence_level, policy),
            citation.source_ref,
            citation.quote,
        ),
    )


def resolve_evidence_conflict(
    positives: Sequence[EvidenceCitation],
    negatives: Sequence[EvidenceCitation],
    policy: MilestoneContractPolicy,
) -> str:
    """Return positive, negative, conflict, or missing for competing evidence."""
    if not positives and not negatives:
        return "missing"
    if positives and not negatives:
        return "positive"
    if negatives and not positives:
        return "negative"

    positive = choose_effective_citations(positives, policy)[0]
    negative = choose_effective_citations(negatives, policy)[0]
    if positive.event_at is None or negative.event_at is None:
        return "conflict"

    positive_at = positive.event_at
    negative_at = negative.event_at
    if positive_at != negative_at:
        return "positive" if positive_at > negative_at else "negative"

    positive_rank = evidence_rank(positive.evidence_level, policy)
    negative_rank = evidence_rank(negative.evidence_level, policy)
    if positive_rank != negative_rank:
        return "positive" if positive_rank < negative_rank else "negative"
    return "conflict"


def citation_is_source_bounded(
    citation: EvidenceCitation,
    *,
    body: str,
    attachment_text: str,
) -> bool:
    """Return whether an exact citation quote exists in its claimed source."""
    quote = citation.quote.strip()
    if not quote:
        return False
    if citation.source_type.value == "email":
        return quote in body
    if citation.source_type.value == "attachment":
        return quote in attachment_text
    return False


def negative_citations_for_facts(
    facts: Sequence[ExtractedProgressFact],
    predicates: Collection[str],
    policy: MilestoneContractPolicy,
    *,
    minimum_level: EvidenceLevel,
    authority_names: Collection[str] = (),
    authority_required: bool = False,
) -> list[EvidenceCitation]:
    authorities = {name.strip().casefold() for name in authority_names}
    selected = []
    for fact in facts:
        if fact.predicate not in predicates:
            continue
        for citation in fact.citations:
            if (
                citation.polarity != EvidencePolarity.NEGATIVE
                or citation.modality != EvidenceModality.CONFIRMED
                or not meets_minimum_evidence_level(citation, minimum_level, policy)
            ):
                continue
            if authority_required and (citation.actor or "").strip().casefold() not in authorities:
                continue
            selected.append(citation)
    return choose_effective_citations(selected, policy)
