"""Detect later-stage email facts that lag the recorded milestone.

The milestone evaluator only scores the current stage's exit criteria. Facts
that belong two or more stages ahead are not current-gate evidence and must
not be treated as "missing M1 evidence". They are a recorded-state conflict.
Adjacent next-stage facts stay advisory; they do not auto-jump and they do
not raise this lag signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.milestone.contracts import MilestoneContractPolicy
from src.models.enums import Milestone
from src.models.progress_assessment import (
    EvidenceCitation,
    EvidenceLevel,
    EvidenceModality,
    EvidencePolarity,
    ExtractedProgressFact,
)

# Extractor predicates that imply a stage but are not contract exit aliases.
_EXTRACTOR_STAGE_ALIASES: dict[str, Milestone] = {
    "conformance_test_completed": Milestone.M5_CONFORMANCE_TEST_CASES,
    "conformance_testing_completed": Milestone.M5_CONFORMANCE_TEST_CASES,
    "production_connectivity_test_planned": (
        Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY
    ),
    "production_connectivity_test_scheduled": (
        Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY
    ),
    "pct_preferred_time_requested": (
        Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY
    ),
    "pct_window_requested": Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY,
    "production_target_comp_id": Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY,
    "production_sender_comp_id": Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY,
}

_USABLE_MODALITIES = frozenset({
    EvidenceModality.CONFIRMED,
    EvidenceModality.IN_PROGRESS,
    EvidenceModality.PLANNED,
})


@dataclass(frozen=True)
class LaterStageLag:
    implied_milestone: Milestone
    citations: tuple[EvidenceCitation, ...]
    predicates: tuple[str, ...]


def predicate_milestones(policy: MilestoneContractPolicy) -> dict[str, Milestone]:
    """Map contract and extractor predicates to the earliest owning milestone."""
    mapping: dict[str, Milestone] = {}
    for contract in policy.milestones:
        predicates: list[str] = []
        for criterion in (
            *contract.required_exit_criteria,
            *contract.optional_exit_criteria,
            *contract.close_criteria,
        ):
            if criterion.predicate:
                predicates.append(criterion.predicate)
            predicates.extend(criterion.accepted_evidence_predicates)
        for predicate in predicates:
            existing = mapping.get(predicate)
            if existing is None or contract.id.index < existing.index:
                mapping[predicate] = contract.id
    for predicate, milestone in _EXTRACTOR_STAGE_ALIASES.items():
        mapping.setdefault(predicate, milestone)
    return mapping


def _usable_citations(fact: ExtractedProgressFact) -> list[EvidenceCitation]:
    selected: list[EvidenceCitation] = []
    for citation in fact.citations:
        if citation.polarity != EvidencePolarity.POSITIVE:
            continue
        if citation.modality not in _USABLE_MODALITIES:
            continue
        if citation.evidence_level == EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT:
            continue
        selected.append(citation)
    return selected


def detect_later_stage_lag(
    *,
    current_milestone: Milestone | None,
    facts: Sequence[ExtractedProgressFact],
    policy: MilestoneContractPolicy,
) -> LaterStageLag | None:
    """Return lag when current facts imply a non-adjacent later milestone."""
    if current_milestone is None:
        return None
    mapping = predicate_milestones(policy)
    implied: Milestone | None = None
    citations: list[EvidenceCitation] = []
    predicates: list[str] = []
    # Current stage and the adjacent next stage are not lag; two-plus ahead is.
    max_non_lag_index = current_milestone.index + 1
    for fact in facts:
        if fact.value is False:
            continue
        stage = mapping.get(fact.predicate)
        if stage is None or stage.index <= max_non_lag_index:
            continue
        usable = _usable_citations(fact)
        if not usable:
            continue
        predicates.append(fact.predicate)
        citations.extend(usable)
        if implied is None or stage.index > implied.index:
            implied = stage
    if implied is None:
        return None
    unique: list[EvidenceCitation] = []
    seen: set[tuple[str, str]] = set()
    for citation in citations:
        key = (citation.source_ref, citation.quote)
        if key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    return LaterStageLag(
        implied_milestone=implied,
        citations=tuple(unique),
        predicates=tuple(dict.fromkeys(predicates)),
    )


def later_stage_reason(current: Milestone, implied: Milestone) -> str:
    return (
        f"Email evidence describes {implied.value} work while the recorded "
        f"milestone is {current.value}."
    )
