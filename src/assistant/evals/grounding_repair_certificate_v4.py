"""Offline-only typed safety certificate for grounding-repair Policy-v4 research.

This module is intentionally isolated from the serving grounding runtime.  It evaluates
whether a deterministic verified-partial Candidate is safe enough to count as a
research-screen selection; it never changes the answer returned to a user.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Literal

DecisionName = Literal["would_skip_repair", "require_repair"]

from .grounding_repair_certificate_v4_rules import (
    AGGREGATION_RE,
    ALLOWED_AGGREGATE_SOURCE_KINDS,
    ALLOWED_INITIAL_ERROR_CODES,
    CITATION_RE,
    COMPLETION_RE,
    CURRENT_RE,
    DEFAULT_METADATA_SCALARS,
    INTERNAL_MARKER_RE,
    MIN_SUBSTANTIVE_RETENTION,
    NUMBER_RE,
    OMISSION_NOTICE,
    POLICY_VERSION,
    RANKING_RE,
    Projection,
    advisory_has_factual_signal,
    claim_tokens,
    is_advisory,
    is_structural,
    projection_text,
    unsupported_tokens,
)

@dataclass(slots=True)
class CertificateProfile:
    """Non-sensitive timing and count diagnostics for one certificate decision."""

    projection_strategy: str = "lazy_cited_limited_projection"
    projection_compute_ns: int = 0
    projection_materialized_count: int = 0
    candidate_line_count: int = 0
    candidate_lines_evaluated: int = 0
    decision_compute_ns: int = 0

    @property
    def projection_compute_ms(self) -> float:
        return self.projection_compute_ns / 1_000_000

    @property
    def decision_compute_ms(self) -> float:
        return self.decision_compute_ns / 1_000_000

    def as_metadata(self) -> dict[str, object]:
        return {
            "projection_strategy": self.projection_strategy,
            "projection_materialized_count": self.projection_materialized_count,
            "candidate_line_count": self.candidate_line_count,
            "candidate_lines_evaluated": self.candidate_lines_evaluated,
            "projection_compute_ms": round(self.projection_compute_ms, 6),
            "decision_compute_ms": round(self.decision_compute_ms, 6),
        }


@dataclass(frozen=True, slots=True)
class CertificateDecision:
    """Plaintext-free Policy-v4 research-screening decision."""

    decision: DecisionName
    reason: str
    would_skip_repair: bool
    risk_codes: tuple[str, ...] = ()
    initial_error_count: int = 0
    candidate_error_count: int = 0
    source_record_count: int = 0
    prepared_substantive_line_count: int = 0
    retained_substantive_line_count: int = 0
    substantive_line_retention: float = 1.0
    valid_cited_line_count: int = 0
    preserved_valid_cited_line_count: int = 0
    candidate_line_count: int = 0
    accepted_cited_line_count: int = 0
    accepted_advisory_line_count: int = 0
    omission_notice_count: int = 0
    projection_materialized_count: int = 0
    projection_compute_ms: float = 0.0
    decision_compute_ms: float = 0.0
    policy_version: str = POLICY_VERSION

    def as_metadata(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "decision": self.decision,
            "reason": self.reason,
            "would_skip_repair": self.would_skip_repair,
            "risk_codes": list(self.risk_codes),
            "initial_error_count": self.initial_error_count,
            "candidate_error_count": self.candidate_error_count,
            "source_record_count": self.source_record_count,
            "prepared_substantive_line_count": self.prepared_substantive_line_count,
            "retained_substantive_line_count": self.retained_substantive_line_count,
            "substantive_line_retention": round(self.substantive_line_retention, 6),
            "valid_cited_line_count": self.valid_cited_line_count,
            "preserved_valid_cited_line_count": self.preserved_valid_cited_line_count,
            "candidate_line_count": self.candidate_line_count,
            "accepted_cited_line_count": self.accepted_cited_line_count,
            "accepted_advisory_line_count": self.accepted_advisory_line_count,
            "omission_notice_count": self.omission_notice_count,
            "projection_materialized_count": self.projection_materialized_count,
            "projection_compute_ms": round(self.projection_compute_ms, 6),
            "decision_compute_ms": round(self.decision_compute_ms, 6),
        }


def evaluate_grounding_repair_certificate_v4(
    *,
    prepared_answer: str,
    candidate_answer: str,
    initial_error_codes: Iterable[str],
    candidate_error_codes: Iterable[str],
    sources: Iterable[dict[str, Any]],
    factual_tools_used: bool,
    profile: CertificateProfile | None = None,
) -> CertificateDecision:
    """Evaluate the locked Policy-v4 typed certificate without runtime side effects."""

    active_profile = profile or CertificateProfile()
    started_ns = time.perf_counter_ns()
    initial_codes = tuple(str(code).strip() for code in initial_error_codes)
    candidate_codes = tuple(str(code).strip() for code in candidate_error_codes)
    source_list = [item for item in sources if isinstance(item, dict)]
    source_records: dict[str, dict[str, Any]] = {}
    duplicate_source_ids = 0
    for source in source_list:
        source_id = str(source.get("source_id") or "").strip()
        if not source_id:
            continue
        if source_id in source_records:
            duplicate_source_ids += 1
        else:
            source_records[source_id] = source

    prepared_lines = [
        line.strip()
        for line in str(prepared_answer or "").splitlines()
        if not is_structural(line) and line.strip() != OMISSION_NOTICE
    ]
    candidate_lines = [
        line.strip()
        for line in str(candidate_answer or "").splitlines()
        if not is_structural(line)
    ]
    active_profile.candidate_line_count = len(candidate_lines)
    prepared_counter = Counter(prepared_lines)
    candidate_counter = Counter(
        line for line in candidate_lines if line in prepared_counter
    )
    retained_count = sum((prepared_counter & candidate_counter).values())
    retention = retained_count / len(prepared_lines) if prepared_lines else 1.0

    valid_prepared_lines = Counter()
    for line in prepared_lines:
        citations = set(CITATION_RE.findall(line))
        if citations and citations.issubset(source_records):
            valid_prepared_lines[line] += 1
    candidate_exact_counter = Counter(candidate_lines)
    preserved_valid = sum((valid_prepared_lines & candidate_exact_counter).values())

    common: dict[str, Any] = {
        "initial_error_count": len(initial_codes),
        "candidate_error_count": len(candidate_codes),
        "source_record_count": len(source_records),
        "prepared_substantive_line_count": len(prepared_lines),
        "retained_substantive_line_count": retained_count,
        "substantive_line_retention": retention,
        "valid_cited_line_count": sum(valid_prepared_lines.values()),
        "preserved_valid_cited_line_count": preserved_valid,
        "candidate_line_count": len(candidate_lines),
    }

    def finish(
        reason: str,
        *,
        risks: Iterable[str] = (),
        accepted_cited: int = 0,
        accepted_advisory: int = 0,
        omission_count: int = 0,
        passed: bool = False,
    ) -> CertificateDecision:
        active_profile.decision_compute_ns = time.perf_counter_ns() - started_ns
        return CertificateDecision(
            decision="would_skip_repair" if passed else "require_repair",
            reason=reason,
            would_skip_repair=passed,
            risk_codes=tuple(sorted(set(risks))),
            accepted_cited_line_count=accepted_cited,
            accepted_advisory_line_count=accepted_advisory,
            omission_notice_count=omission_count,
            projection_materialized_count=active_profile.projection_materialized_count,
            projection_compute_ms=active_profile.projection_compute_ms,
            decision_compute_ms=active_profile.decision_compute_ms,
            **common,
        )

    if not source_records:
        return finish("no_source_records", risks=("source_records_unavailable",))
    if duplicate_source_ids:
        return finish("duplicate_source_id", risks=("source_identity_ambiguous",))
    if len(initial_codes) > 2:
        return finish("too_many_initial_errors")
    if not initial_codes:
        return finish("missing_initial_error_taxonomy")
    if "missing_citations" in initial_codes:
        return finish("missing_initial_error_taxonomy")
    if "unknown" in initial_codes:
        return finish("unknown_error_taxonomy")
    if not set(initial_codes).issubset(ALLOWED_INITIAL_ERROR_CODES):
        return finish("unsupported_error_signature")
    if candidate_codes:
        return finish("candidate_validation_failed")
    if not str(candidate_answer or "").strip():
        return finish("candidate_empty")
    if INTERNAL_MARKER_RE.search(candidate_answer or ""):
        return finish("candidate_internal_marker")

    candidate_citations = set(CITATION_RE.findall(candidate_answer or ""))
    if not candidate_citations.issubset(source_records):
        return finish(
            "candidate_unknown_citation",
            risks=("unknown_citation",),
        )
    if preserved_valid != sum(valid_prepared_lines.values()):
        return finish("valid_cited_lines_not_preserved")
    if retention < MIN_SUBSTANTIVE_RETENTION:
        return finish("substantive_retention_below_threshold")

    projection_cache: dict[str, Projection] = {}

    def projection(source_id: str) -> Projection:
        cached = projection_cache.get(source_id)
        if cached is not None:
            return cached
        projection_started_ns = time.perf_counter_ns()
        source = source_records[source_id]
        text = projection_text(source, DEFAULT_METADATA_SCALARS)
        cached = Projection(
            text=text,
            tokens=frozenset(claim_tokens(text)),
            numbers=frozenset(NUMBER_RE.findall(text)),
            kind=str(source.get("kind") or "").strip(),
        )
        active_profile.projection_compute_ns += time.perf_counter_ns() - projection_started_ns
        active_profile.projection_materialized_count += 1
        projection_cache[source_id] = cached
        return cached

    remaining_exact = Counter(prepared_lines)
    accepted_cited = 0
    accepted_advisory = 0
    omission_count = 0
    for line in candidate_lines:
        active_profile.candidate_lines_evaluated += 1
        if line == OMISSION_NOTICE:
            omission_count += 1
            continue
        citations = set(CITATION_RE.findall(line))
        claim = CITATION_RE.sub("", line).strip()
        if not citations:
            if not is_advisory(line):
                return finish(
                    "candidate_line_outside_typed_certificate",
                    risks=("untyped_uncited_line",),
                    accepted_cited=accepted_cited,
                    accepted_advisory=accepted_advisory,
                    omission_count=omission_count,
                )
            if advisory_has_factual_signal(line):
                return finish(
                    "advisory_contains_factual_signal",
                    risks=("advisory_factual_signal",),
                    accepted_cited=accepted_cited,
                    accepted_advisory=accepted_advisory,
                    omission_count=omission_count,
                )
            accepted_advisory += 1
            continue

        if remaining_exact[line] <= 0:
            return finish(
                "candidate_cited_line_not_exact_prepared_line",
                risks=("cited_line_changed_or_added",),
                accepted_cited=accepted_cited,
                accepted_advisory=accepted_advisory,
                omission_count=omission_count,
            )
        remaining_exact[line] -= 1
        if CURRENT_RE.search(claim):
            return finish(
                "current_state_claim_vetoed",
                risks=("current_latest_asof_claim",),
                accepted_cited=accepted_cited,
                accepted_advisory=accepted_advisory,
                omission_count=omission_count,
            )
        if COMPLETION_RE.search(claim):
            return finish(
                "completion_claim_vetoed",
                risks=("completion_readiness_golive_claim",),
                accepted_cited=accepted_cited,
                accepted_advisory=accepted_advisory,
                omission_count=omission_count,
            )
        if is_advisory(claim):
            return finish(
                "cited_recommendation_as_fact_vetoed",
                risks=("cited_recommendation_as_fact",),
                accepted_cited=accepted_cited,
                accepted_advisory=accepted_advisory,
                omission_count=omission_count,
            )
        if RANKING_RE.search(claim):
            return finish(
                "ranking_comparative_claim_vetoed",
                risks=("ranking_comparative_claim",),
                accepted_cited=accepted_cited,
                accepted_advisory=accepted_advisory,
                omission_count=omission_count,
            )

        is_aggregate = bool(AGGREGATION_RE.search(claim))
        if is_aggregate:
            source_kind = str(source_records[next(iter(citations))].get("kind") or "") if len(citations) == 1 else ""
            if len(citations) != 1 or source_kind not in ALLOWED_AGGREGATE_SOURCE_KINDS:
                return finish(
                    "aggregation_source_contract_failed",
                    risks=("aggregation_source_kind_or_cardinality",),
                    accepted_cited=accepted_cited,
                    accepted_advisory=accepted_advisory,
                    omission_count=omission_count,
                )

        cited_projections = [projection(source_id) for source_id in sorted(citations)]
        supported_numbers = set().union(*(item.numbers for item in cited_projections))
        if not set(NUMBER_RE.findall(claim)).issubset(supported_numbers):
            return finish(
                "numeric_or_date_support_mismatch",
                risks=("numeric_date_percentage_mismatch",),
                accepted_cited=accepted_cited,
                accepted_advisory=accepted_advisory,
                omission_count=omission_count,
            )

        if is_aggregate:
            unsupported = unsupported_tokens(claim, cited_projections[0])
            if unsupported:
                return finish(
                    "aggregation_claim_not_fully_supported",
                    risks=("aggregation_token_support_mismatch",),
                    accepted_cited=accepted_cited,
                    accepted_advisory=accepted_advisory,
                    omission_count=omission_count,
                )
        accepted_cited += 1

    if factual_tools_used and not accepted_cited:
        return finish(
            "candidate_missing_valid_citation",
            accepted_cited=accepted_cited,
            accepted_advisory=accepted_advisory,
            omission_count=omission_count,
        )
    return finish(
        "certificate_passed_policy_v4",
        accepted_cited=accepted_cited,
        accepted_advisory=accepted_advisory,
        omission_count=omission_count,
        passed=True,
    )


__all__ = [
    "CertificateDecision",
    "CertificateProfile",
    "MIN_SUBSTANTIVE_RETENTION",
    "POLICY_VERSION",
    "evaluate_grounding_repair_certificate_v4",
]
