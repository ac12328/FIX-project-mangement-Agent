"""Offline-only exact-reconstruction certificate for grounding-repair Policy-v7."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from .grounding_repair_candidate_v7 import (
    POLICY_VERSION_V7,
    CanonicalCandidateV7,
    build_grounding_repair_candidate_v7,
)
from .grounding_repair_certificate_v5_rules import (
    ALLOWED_INITIAL_ERROR_CODES,
    CITATION_RE,
    INTERNAL_MARKER_RE,
)

DecisionNameV7 = Literal["would_skip_repair", "require_repair"]


@dataclass(slots=True)
class CertificateProfileV7:
    candidate_build_compute_ns: int = 0
    decision_compute_ns: int = 0
    candidate_build_strategy: str = "complete_query_canonical_projection"
    source_row_count: int = 0
    emitted_row_count: int = 0
    omitted_terminal_row_count: int = 0

    @property
    def candidate_build_compute_ms(self) -> float:
        return self.candidate_build_compute_ns / 1_000_000

    @property
    def decision_compute_ms(self) -> float:
        return self.decision_compute_ns / 1_000_000

    def as_metadata(self) -> dict[str, int | float | str]:
        return {
            "candidate_build_strategy": self.candidate_build_strategy,
            "source_row_count": self.source_row_count,
            "emitted_row_count": self.emitted_row_count,
            "omitted_terminal_row_count": self.omitted_terminal_row_count,
            "candidate_build_compute_ms": round(self.candidate_build_compute_ms, 6),
            "decision_compute_ms": round(self.decision_compute_ms, 6),
        }


@dataclass(frozen=True, slots=True)
class CertificateDecisionV7:
    decision: DecisionNameV7
    reason: str
    would_skip_repair: bool
    risk_codes: tuple[str, ...] = ()
    initial_error_count: int = 0
    candidate_error_count: int = 0
    source_row_count: int = 0
    emitted_row_count: int = 0
    omitted_terminal_row_count: int = 0
    cited_source_count: int = 0
    decision_compute_ms: float = 0.0
    policy_version: str = POLICY_VERSION_V7

    def as_metadata(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "decision": self.decision,
            "reason": self.reason,
            "would_skip_repair": self.would_skip_repair,
            "risk_codes": list(self.risk_codes),
            "initial_error_count": self.initial_error_count,
            "candidate_error_count": self.candidate_error_count,
            "source_row_count": self.source_row_count,
            "emitted_row_count": self.emitted_row_count,
            "omitted_terminal_row_count": self.omitted_terminal_row_count,
            "cited_source_count": self.cited_source_count,
            "decision_compute_ms": round(self.decision_compute_ms, 6),
        }


def evaluate_grounding_repair_certificate_v7(
    *,
    candidate_answer: str,
    initial_error_codes: Iterable[str],
    candidate_error_codes: Iterable[str],
    question: str,
    sources: Iterable[dict[str, Any]],
    tool_results: Iterable[dict[str, Any]] = (),
    canonical: CanonicalCandidateV7 | None = None,
    profile: CertificateProfileV7 | None = None,
) -> CertificateDecisionV7:
    """Select only an exact canonical reconstruction of a complete structured query."""

    started_ns = time.perf_counter_ns()
    active_profile = profile or CertificateProfileV7()
    initial_codes = tuple(sorted(str(item or "unknown") for item in initial_error_codes))
    candidate_codes = tuple(sorted(str(item or "unknown") for item in candidate_error_codes))
    source_list = [item for item in sources if isinstance(item, dict)]
    tool_result_list = [item for item in tool_results if isinstance(item, dict)]

    build_started = time.perf_counter_ns()
    built = build_grounding_repair_candidate_v7(
        question=question,
        sources=source_list,
        tool_results=tool_result_list,
    )
    active_profile.candidate_build_compute_ns = time.perf_counter_ns() - build_started
    active_profile.source_row_count = built.source_row_count
    active_profile.emitted_row_count = built.emitted_row_count
    active_profile.omitted_terminal_row_count = built.omitted_terminal_row_count

    def finish(
        reason: str,
        *,
        risks: Iterable[str] = (),
        passed: bool = False,
    ) -> CertificateDecisionV7:
        active_profile.decision_compute_ns = time.perf_counter_ns() - started_ns
        return CertificateDecisionV7(
            decision="would_skip_repair" if passed else "require_repair",
            reason=reason,
            would_skip_repair=passed,
            risk_codes=tuple(sorted(set(str(item) for item in risks))),
            initial_error_count=len(initial_codes),
            candidate_error_count=len(candidate_codes),
            source_row_count=built.source_row_count,
            emitted_row_count=built.emitted_row_count,
            omitted_terminal_row_count=built.omitted_terminal_row_count,
            cited_source_count=built.cited_source_count,
            decision_compute_ms=active_profile.decision_compute_ms,
        )

    if not initial_codes or not set(initial_codes).issubset(ALLOWED_INITIAL_ERROR_CODES):
        return finish("unsupported_initial_error_signature")
    if canonical is not None and canonical != built:
        return finish(
            "provided_canonical_projection_mismatch",
            risks=("reconstruction_mismatch",),
        )
    if not built.eligible:
        return finish(built.reason)
    if candidate_codes:
        return finish("candidate_validation_failed")
    if not candidate_answer.strip() or candidate_answer != built.candidate_answer:
        return finish("candidate_not_exact_canonical_projection", risks=("reconstruction_mismatch",))
    if INTERNAL_MARKER_RE.search(candidate_answer):
        return finish("candidate_internal_marker")
    known_sources = {
        str(item.get("source_id") or "").strip()
        for item in source_list
        if str(item.get("source_id") or "").strip()
    }
    citations = set(CITATION_RE.findall(candidate_answer))
    if len(citations) != built.cited_source_count or not citations.issubset(known_sources):
        return finish("candidate_citation_set_mismatch", risks=("citation_identity_mismatch",))
    return finish("certificate_passed_policy_v7", passed=True)


__all__ = [
    "CertificateDecisionV7",
    "CertificateProfileV7",
    "DecisionNameV7",
    "POLICY_VERSION_V7",
    "evaluate_grounding_repair_certificate_v7",
]
