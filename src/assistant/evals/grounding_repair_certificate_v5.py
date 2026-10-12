"""Offline-only typed safety certificate for grounding-repair Policy-v5 research.

Policy-v5 remains isolated from the serving grounding runtime.  It certifies only
observable, source-linked output structure and never changes a user-visible answer.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from .grounding_repair_certificate_v5_rules import (
    ALLOWED_INITIAL_ERROR_CODES,
    CITATION_RE,
    INTERNAL_MARKER_RE,
    LineContractCacheV5,
    MIN_SUBSTANTIVE_RETENTION,
    OMISSION_NOTICE,
    POLICY_VERSION,
    ProjectionCacheV5,
    SourceProjectionV5,
    build_source_projections_v5,
    classify_line_v5,
    line_contract_v5,
)

DecisionName = Literal["would_skip_repair", "require_repair"]


@dataclass(slots=True)
class CertificateProfileV5:
    """Non-sensitive timing and count diagnostics for one certificate decision."""

    projection_strategy: str = "typed_direct_source_projection"
    projection_compute_ns: int = 0
    projection_materialized_count: int = 0
    candidate_line_count: int = 0
    candidate_lines_evaluated: int = 0
    candidate_filter_compute_ns: int = 0
    candidate_filter_strategy: str = "typed_filter_inline"
    candidate_filter_input_line_count: int = 0
    candidate_filter_retained_line_count: int = 0
    candidate_filter_dropped_factual_line_count: int = 0
    decision_compute_ns: int = 0

    @property
    def projection_compute_ms(self) -> float:
        return self.projection_compute_ns / 1_000_000

    @property
    def candidate_filter_compute_ms(self) -> float:
        return self.candidate_filter_compute_ns / 1_000_000

    @property
    def decision_compute_ms(self) -> float:
        return self.decision_compute_ns / 1_000_000

    def as_metadata(self) -> dict[str, object]:
        return {
            "projection_strategy": self.projection_strategy,
            "projection_materialized_count": self.projection_materialized_count,
            "candidate_line_count": self.candidate_line_count,
            "candidate_lines_evaluated": self.candidate_lines_evaluated,
            "candidate_filter_input_line_count": self.candidate_filter_input_line_count,
            "candidate_filter_retained_line_count": self.candidate_filter_retained_line_count,
            "candidate_filter_dropped_factual_line_count": self.candidate_filter_dropped_factual_line_count,
            "candidate_filter_strategy": self.candidate_filter_strategy,
            "projection_compute_ms": round(self.projection_compute_ms, 6),
            "candidate_filter_compute_ms": round(self.candidate_filter_compute_ms, 6),
            "decision_compute_ms": round(self.decision_compute_ms, 6),
        }


@dataclass(frozen=True, slots=True)
class CertificateDecisionV5:
    """Plaintext-free Policy-v5 research-screening decision."""

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
    accepted_exact_cited_line_count: int = 0
    accepted_added_cited_line_count: int = 0
    accepted_advisory_line_count: int = 0
    accepted_structural_line_count: int = 0
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
            "accepted_exact_cited_line_count": self.accepted_exact_cited_line_count,
            "accepted_added_cited_line_count": self.accepted_added_cited_line_count,
            "accepted_advisory_line_count": self.accepted_advisory_line_count,
            "accepted_structural_line_count": self.accepted_structural_line_count,
            "omission_notice_count": self.omission_notice_count,
            "projection_materialized_count": self.projection_materialized_count,
            "projection_compute_ms": round(self.projection_compute_ms, 6),
            "decision_compute_ms": round(self.decision_compute_ms, 6),
        }


def _normalized_codes(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())


def evaluate_grounding_repair_certificate_v5(
    *,
    prepared_answer: str,
    candidate_answer: str,
    initial_error_codes: Iterable[str],
    candidate_error_codes: Iterable[str],
    sources: Iterable[dict[str, Any]],
    factual_tools_used: bool,
    question: str = "",
    tool_results: Iterable[dict[str, Any]] = (),
    projection_cache: ProjectionCacheV5 | None = None,
    contract_cache: LineContractCacheV5 | None = None,
    profile: CertificateProfileV5 | None = None,
) -> CertificateDecisionV5:
    """Evaluate the deterministic Policy-v5 certificate without runtime side effects."""

    active_profile = profile or CertificateProfileV5()
    if projection_cache is not None:
        active_profile.projection_strategy = "typed_preindexed_source_projection"
    started_ns = time.perf_counter_ns()
    initial_codes = _normalized_codes(initial_error_codes)
    candidate_codes = _normalized_codes(candidate_error_codes)
    source_list = [item for item in sources if isinstance(item, dict)]
    tool_result_list = [item for item in tool_results if isinstance(item, dict)]

    known_source_ids = (
        frozenset(projection_cache.records)
        if projection_cache is not None
        else frozenset(
            str(item.get("source_id") or "").strip() for item in source_list
            if str(item.get("source_id") or "").strip()
        )
    )
    cited_source_ids = frozenset(CITATION_RE.findall(
        f"{prepared_answer or ''}\n{candidate_answer or ''}"
    ))
    known_project_ids = (
        projection_cache.known_project_ids
        if projection_cache is not None
        else frozenset(
            str(item.get("project_id") or "").upper().replace("_", "-")
            for item in source_list if str(item.get("project_id") or "").strip()
        )
    )
    projection_started_ns = time.perf_counter_ns()
    projections, duplicate_source_ids = build_source_projections_v5(
        source_list, tool_result_list, materialize_source_ids=cited_source_ids, cache=projection_cache
    )
    active_profile.projection_compute_ns = time.perf_counter_ns() - projection_started_ns
    active_profile.projection_materialized_count = len(projections)

    prepared_lines = [line.strip() for line in str(prepared_answer or "").splitlines()]
    candidate_lines = [line.strip() for line in str(candidate_answer or "").splitlines()]
    active_profile.candidate_line_count = len(candidate_lines)

    prepared_substantive = [
        line for line in prepared_lines
        if classify_line_v5(line) not in {"blank", "structural", "omission"}
    ]
    candidate_substantive = [
        line for line in candidate_lines
        if classify_line_v5(line) not in {"blank", "structural", "omission"}
    ]
    prepared_counter = Counter(prepared_substantive)
    candidate_counter = Counter(line for line in candidate_substantive if line in prepared_counter)
    retained_count = sum((prepared_counter & candidate_counter).values())
    retention = retained_count / len(prepared_substantive) if prepared_substantive else 1.0

    valid_prepared_lines = Counter()
    for line in prepared_substantive:
        citations = set(CITATION_RE.findall(line))
        if not citations or not citations.issubset(projections):
            continue
        claim = CITATION_RE.sub("", line).strip()
        evaluator = contract_cache.evaluate if contract_cache is not None else line_contract_v5
        supported, _, _ = evaluator(
            question=str(question or ""),
            claim=claim,
            projections=(projections[source_id] for source_id in sorted(citations)),
            tool_results=tool_result_list,
            known_project_ids=known_project_ids,
            all_projects_requested=(
                projection_cache.all_projects_requested
                if projection_cache is not None
                else False
            ),
        )
        if supported:
            valid_prepared_lines[line] += 1
    candidate_exact_counter = Counter(candidate_substantive)
    preserved_valid = sum((valid_prepared_lines & candidate_exact_counter).values())

    common: dict[str, Any] = {
        "initial_error_count": len(initial_codes),
        "candidate_error_count": len(candidate_codes),
        "source_record_count": len(projections),
        "prepared_substantive_line_count": len(prepared_substantive),
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
        exact_cited: int = 0,
        added_cited: int = 0,
        advisory: int = 0,
        structural: int = 0,
        omission: int = 0,
        passed: bool = False,
    ) -> CertificateDecisionV5:
        active_profile.decision_compute_ns = time.perf_counter_ns() - started_ns
        return CertificateDecisionV5(
            decision="would_skip_repair" if passed else "require_repair",
            reason=reason,
            would_skip_repair=passed,
            risk_codes=tuple(sorted(set(risks))),
            accepted_exact_cited_line_count=exact_cited,
            accepted_added_cited_line_count=added_cited,
            accepted_advisory_line_count=advisory,
            accepted_structural_line_count=structural,
            omission_notice_count=omission,
            projection_materialized_count=active_profile.projection_materialized_count,
            projection_compute_ms=active_profile.projection_compute_ms,
            decision_compute_ms=active_profile.decision_compute_ms,
            **common,
        )

    if not projections:
        return finish("no_source_records", risks=("source_records_unavailable",))
    if duplicate_source_ids:
        return finish("duplicate_source_id", risks=("source_identity_ambiguous",))
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
    if not candidate_citations.issubset(known_source_ids):
        return finish("candidate_unknown_citation", risks=("unknown_citation",))
    candidate_query_scopes = {
        projection.query_scope
        for source_id in candidate_citations
        if (projection := projections.get(source_id)) is not None
        and projection.query_scope
    }
    if {"active_projects", "all_projects"}.issubset(candidate_query_scopes):
        return finish(
            "candidate_conflicting_portfolio_scope",
            risks=("candidate_portfolio_scope_ambiguous",),
        )
    if preserved_valid != sum(valid_prepared_lines.values()):
        return finish("valid_cited_lines_not_preserved")
    if retention < MIN_SUBSTANTIVE_RETENTION:
        return finish("substantive_retention_below_threshold")

    remaining_exact = Counter(prepared_substantive)
    exact_cited = added_cited = advisory = structural = omission = 0
    for line in candidate_lines:
        active_profile.candidate_lines_evaluated += 1
        line_kind = classify_line_v5(line)
        if line_kind == "blank":
            continue
        if line_kind == "structural":
            structural += 1
            continue
        if line_kind == "omission":
            omission += 1
            continue
        citations = set(CITATION_RE.findall(line))
        if not citations:
            if line_kind == "advisory":
                advisory += 1
                continue
            return finish(
                "candidate_line_outside_typed_certificate",
                risks=("untyped_uncited_line",),
                exact_cited=exact_cited,
                added_cited=added_cited,
                advisory=advisory,
                structural=structural,
                omission=omission,
            )
        if not citations.issubset(projections):
            return finish(
                "candidate_unknown_citation",
                risks=("unknown_citation",),
                exact_cited=exact_cited,
                added_cited=added_cited,
                advisory=advisory,
                structural=structural,
                omission=omission,
            )

        claim = CITATION_RE.sub("", line).strip()
        evaluator = contract_cache.evaluate if contract_cache is not None else line_contract_v5
        supported, reason, risks = evaluator(
            question=str(question or ""),
            claim=claim,
            projections=(projections[source_id] for source_id in sorted(citations)),
            tool_results=tool_result_list,
            known_project_ids=known_project_ids,
            all_projects_requested=(projection_cache.all_projects_requested if projection_cache is not None else False),
        )
        if not supported:
            return finish(
                reason,
                risks=risks,
                exact_cited=exact_cited,
                added_cited=added_cited,
                advisory=advisory,
                structural=structural,
                omission=omission,
            )
        if remaining_exact[line] > 0:
            remaining_exact[line] -= 1
            exact_cited += 1
        else:
            added_cited += 1

    if factual_tools_used and not (exact_cited or added_cited):
        return finish(
            "candidate_missing_valid_citation",
            exact_cited=exact_cited,
            added_cited=added_cited,
            advisory=advisory,
            structural=structural,
            omission=omission,
        )
    return finish(
        "certificate_passed_policy_v5",
        exact_cited=exact_cited,
        added_cited=added_cited,
        advisory=advisory,
        structural=structural,
        omission=omission,
        passed=True,
    )


__all__ = [
    "CertificateDecisionV5",
    "CertificateProfileV5",
    "MIN_SUBSTANTIVE_RETENTION",
    "POLICY_VERSION",
    "evaluate_grounding_repair_certificate_v5",
]
