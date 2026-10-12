"""Offline-only answer-level safety certificate for grounding-repair Policy-v6."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from .grounding_repair_candidate_v6 import (
    POLICY_VERSION_V6,
    answer_contract_v6,
)
from .grounding_repair_certificate_v5_projection import ProjectionCacheV5
from .grounding_repair_certificate_v5_rules import (
    ALLOWED_INITIAL_ERROR_CODES,
    CITATION_RE,
    INTERNAL_MARKER_RE,
    LineContractCacheV5,
    OMISSION_NOTICE,
    classify_line_v5,
    line_contract_v5,
)

MIN_FACTUAL_LINE_COUNT_V6 = 3
MIN_FACTUAL_RETENTION_V6 = 0.80
DecisionNameV6 = Literal["would_skip_repair", "require_repair"]


@dataclass(slots=True)
class CertificateProfileV6:
    projection_compute_ns: int = 0
    candidate_filter_compute_ns: int = 0
    answer_contract_compute_ns: int = 0
    decision_compute_ns: int = 0
    candidate_filter_strategy: str = "section_coherent_inline"
    projection_strategy: str = "typed_preindexed_source_projection"
    projection_materialized_count: int = 0
    source_factual_line_count: int = 0
    retained_factual_line_count: int = 0
    candidate_filter_input_line_count: int = 0
    candidate_filter_retained_line_count: int = 0
    candidate_filter_dropped_factual_line_count: int = 0

    @property
    def projection_compute_ms(self) -> float:
        return self.projection_compute_ns / 1_000_000

    @property
    def candidate_filter_compute_ms(self) -> float:
        return self.candidate_filter_compute_ns / 1_000_000

    @property
    def answer_contract_compute_ms(self) -> float:
        return self.answer_contract_compute_ns / 1_000_000

    @property
    def decision_compute_ms(self) -> float:
        return self.decision_compute_ns / 1_000_000

    def as_metadata(self) -> dict[str, int | float | str]:
        return {
            "projection_strategy": self.projection_strategy,
            "projection_materialized_count": self.projection_materialized_count,
            "candidate_filter_strategy": self.candidate_filter_strategy,
            "candidate_filter_input_line_count": self.candidate_filter_input_line_count,
            "candidate_filter_retained_line_count": self.candidate_filter_retained_line_count,
            "candidate_filter_dropped_factual_line_count": self.candidate_filter_dropped_factual_line_count,
            "source_factual_line_count": self.source_factual_line_count,
            "retained_factual_line_count": self.retained_factual_line_count,
            "candidate_filter_compute_ms": round(self.candidate_filter_compute_ms, 6),
            "answer_contract_compute_ms": round(self.answer_contract_compute_ms, 6),
            "decision_compute_ms": round(self.decision_compute_ms, 6),
        }


@dataclass(frozen=True, slots=True)
class CertificateDecisionV6:
    decision: DecisionNameV6
    reason: str
    would_skip_repair: bool
    risk_codes: tuple[str, ...] = ()
    initial_error_count: int = 0
    candidate_error_count: int = 0
    source_factual_line_count: int = 0
    retained_factual_line_count: int = 0
    factual_line_retention: float = 0.0
    accepted_factual_line_count: int = 0
    accepted_structural_line_count: int = 0
    omission_notice_count: int = 0
    section_count: int = 0
    query_summary_line_count: int = 0
    direct_source_line_count: int = 0
    mixed_citation_line_count: int = 0
    decision_compute_ms: float = 0.0
    policy_version: str = POLICY_VERSION_V6

    def as_metadata(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "decision": self.decision,
            "reason": self.reason,
            "would_skip_repair": self.would_skip_repair,
            "risk_codes": list(self.risk_codes),
            "initial_error_count": self.initial_error_count,
            "candidate_error_count": self.candidate_error_count,
            "source_factual_line_count": self.source_factual_line_count,
            "retained_factual_line_count": self.retained_factual_line_count,
            "factual_line_retention": round(self.factual_line_retention, 6),
            "accepted_factual_line_count": self.accepted_factual_line_count,
            "accepted_structural_line_count": self.accepted_structural_line_count,
            "omission_notice_count": self.omission_notice_count,
            "section_count": self.section_count,
            "query_summary_line_count": self.query_summary_line_count,
            "direct_source_line_count": self.direct_source_line_count,
            "mixed_citation_line_count": self.mixed_citation_line_count,
            "decision_compute_ms": round(self.decision_compute_ms, 6),
        }


def _codes(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(str(value or "unknown") for value in values))


def _factual_lines(answer: str) -> list[str]:
    return [
        line.strip()
        for line in str(answer or "").splitlines()
        if classify_line_v5(line.strip()) == "factual"
    ]


def evaluate_grounding_repair_certificate_v6(
    *,
    source_candidate_answer: str,
    candidate_answer: str,
    initial_error_codes: Iterable[str],
    candidate_error_codes: Iterable[str],
    sources: Iterable[dict[str, Any]],
    factual_tools_used: bool,
    question: str = "",
    tool_results: Iterable[dict[str, Any]] = (),
    projection_cache: ProjectionCacheV5 | None = None,
    contract_cache: LineContractCacheV5 | None = None,
    profile: CertificateProfileV6 | None = None,
) -> CertificateDecisionV6:
    """Require both exact line support and self-contained answer-level scope."""

    active_profile = profile or CertificateProfileV6()
    started_ns = time.perf_counter_ns()
    initial_codes = _codes(initial_error_codes)
    candidate_codes = _codes(candidate_error_codes)
    source_list = [item for item in sources if isinstance(item, dict)]
    tool_result_list = [item for item in tool_results if isinstance(item, dict)]
    cache = projection_cache or ProjectionCacheV5.build(source_list, tool_result_list)
    known_source_ids = frozenset(cache.records)
    source_factual = _factual_lines(source_candidate_answer)
    retained_factual = _factual_lines(candidate_answer)
    retention = len(retained_factual) / len(source_factual) if source_factual else 0.0
    active_profile.source_factual_line_count = len(source_factual)
    active_profile.retained_factual_line_count = len(retained_factual)

    answer_started_ns = time.perf_counter_ns()
    answer_decision = answer_contract_v6(
        candidate_answer=candidate_answer,
        projection_cache=cache,
    )
    active_profile.answer_contract_compute_ns = time.perf_counter_ns() - answer_started_ns
    structural_count = sum(
        classify_line_v5(line.strip()) == "structural"
        for line in str(candidate_answer or "").splitlines()
    )
    omission_count = sum(
        line.strip() == OMISSION_NOTICE
        for line in str(candidate_answer or "").splitlines()
    )

    def finish(
        reason: str,
        *,
        risks: Iterable[str] = (),
        passed: bool = False,
    ) -> CertificateDecisionV6:
        active_profile.decision_compute_ns = time.perf_counter_ns() - started_ns
        return CertificateDecisionV6(
            decision="would_skip_repair" if passed else "require_repair",
            reason=reason,
            would_skip_repair=passed,
            risk_codes=tuple(sorted(set(risks))),
            initial_error_count=len(initial_codes),
            candidate_error_count=len(candidate_codes),
            source_factual_line_count=len(source_factual),
            retained_factual_line_count=len(retained_factual),
            factual_line_retention=retention,
            accepted_factual_line_count=answer_decision.factual_line_count,
            accepted_structural_line_count=structural_count,
            omission_notice_count=omission_count,
            section_count=answer_decision.section_count,
            query_summary_line_count=answer_decision.query_summary_line_count,
            direct_source_line_count=answer_decision.direct_source_line_count,
            mixed_citation_line_count=answer_decision.mixed_citation_line_count,
            decision_compute_ms=active_profile.decision_compute_ms,
        )

    if not source_list:
        return finish("missing_source_records", risks=("source_records_unavailable",))
    if cache.duplicate_source_id_count:
        return finish("duplicate_source_id", risks=("source_identity_ambiguous",))
    if not initial_codes or not set(initial_codes).issubset(ALLOWED_INITIAL_ERROR_CODES):
        return finish("unsupported_initial_error_signature")
    if candidate_codes:
        return finish("candidate_validation_failed")
    if not str(candidate_answer or "").strip():
        return finish("candidate_empty")
    if INTERNAL_MARKER_RE.search(candidate_answer or ""):
        return finish("candidate_internal_marker")
    citations = set(CITATION_RE.findall(candidate_answer or ""))
    if not citations.issubset(known_source_ids):
        return finish("candidate_unknown_citation", risks=("unknown_citation",))
    if factual_tools_used and not citations:
        return finish("candidate_missing_valid_citation")
    if omission_count > 1:
        return finish("multiple_omission_notices", risks=("answer_structure_ambiguous",))
    if retention < 1.0 and omission_count != 1:
        return finish("missing_omission_notice", risks=("omission_not_disclosed",))
    if len(retained_factual) < MIN_FACTUAL_LINE_COUNT_V6:
        return finish("minimum_factual_line_count_not_met")
    if retention < MIN_FACTUAL_RETENTION_V6:
        return finish("factual_retention_below_threshold")
    if not answer_decision.accepted:
        return finish(answer_decision.reason, risks=answer_decision.risk_codes)

    projections = cache.materialize(citations)
    evaluator = contract_cache.evaluate if contract_cache is not None else line_contract_v5
    for line in retained_factual:
        line_citations = set(CITATION_RE.findall(line))
        if not line_citations or not line_citations.issubset(projections):
            return finish("candidate_line_outside_typed_certificate")
        claim = CITATION_RE.sub("", line).strip()
        supported, reason, risks = evaluator(
            question=str(question or ""),
            claim=claim,
            projections=(projections[source_id] for source_id in sorted(line_citations)),
            tool_results=tool_result_list,
            known_project_ids=cache.known_project_ids,
            all_projects_requested=cache.all_projects_requested,
        )
        if not supported:
            return finish(reason, risks=risks)
    return finish("certificate_passed_policy_v6", passed=True)


__all__ = [
    "CertificateDecisionV6", "CertificateProfileV6", "MIN_FACTUAL_LINE_COUNT_V6",
    "MIN_FACTUAL_RETENTION_V6", "POLICY_VERSION_V6",
    "evaluate_grounding_repair_certificate_v6",
]
