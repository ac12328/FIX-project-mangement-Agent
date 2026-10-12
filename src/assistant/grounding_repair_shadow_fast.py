"""Low-latency decision path for the shadow-only grounding-repair policy."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from src.assistant.grounding_repair_shadow import (
    GROUNDING_REPAIR_MIN_CITATION_TOKEN_SUPPORT,
    GROUNDING_REPAIR_SHADOW_MIN_RETENTION,
    GROUNDING_REPAIR_SHADOW_POLICY_VERSION,
    GroundingRepairShadowDecision,
    GroundingSemanticSourceSupport,
    _ALLOWED_INITIAL_ERROR_CODES,
    _INTERNAL_TOOL_ARTIFACT_RE,
    _NUMBER_RE,
    _SCOPE_AGGREGATION_RE,
    _answer_analysis,
    _retained_count,
    _semantic_tokens,
    _serialize_source_record,
)


@dataclass(slots=True)
class GroundingRepairShadowProfile:
    """Non-sensitive timing and count diagnostics for the fast decision path."""

    semantic_support_strategy: str = "lazy_first_risk"
    source_support_compute_ns: int = 0
    source_support_materialized_count: int = 0
    semantic_line_count: int = 0
    semantic_lines_evaluated: int = 0
    semantic_analysis_complete: bool = False
    semantic_short_circuit: bool = False

    @property
    def source_support_compute_ms(self) -> float:
        return self.source_support_compute_ns / 1_000_000

    def as_metadata(self) -> dict[str, object]:
        return {
            "semantic_support_strategy": self.semantic_support_strategy,
            "semantic_support_materialized_count": self.source_support_materialized_count,
            "semantic_line_count": self.semantic_line_count,
            "semantic_lines_evaluated": self.semantic_lines_evaluated,
            "semantic_analysis_complete": self.semantic_analysis_complete,
            "semantic_short_circuit": self.semantic_short_circuit,
        }


def _support_for_source(
    source_id: str,
    source_records: dict[str, dict[str, Any]],
    cache: dict[str, GroundingSemanticSourceSupport],
    profile: GroundingRepairShadowProfile,
) -> GroundingSemanticSourceSupport:
    cached = cache.get(source_id)
    if cached is not None:
        return cached
    started_ns = time.perf_counter_ns()
    source = source_records[source_id]
    serialized = _serialize_source_record(source)
    cached = GroundingSemanticSourceSupport(
        tokens=frozenset(_semantic_tokens(serialized)),
        numbers=frozenset(_NUMBER_RE.findall(serialized)),
        kind=str(source.get("kind") or ""),
    )
    profile.source_support_compute_ns += time.perf_counter_ns() - started_ns
    profile.source_support_materialized_count += 1
    cache[source_id] = cached
    return cached


def _decision(
    reason: str,
    common: dict[str, object],
    *,
    would_skip_repair: bool = False,
) -> GroundingRepairShadowDecision:
    return GroundingRepairShadowDecision(
        decision="would_skip_repair" if would_skip_repair else "require_repair",
        reason=reason,
        **{**common, "would_skip_repair": would_skip_repair},
    )


def evaluate_grounding_repair_shadow_fast(
    *,
    prepared_answer: str,
    initial_error_codes: Iterable[str],
    candidate_answer: str,
    candidate_error_codes: Iterable[str],
    source_ids: Iterable[str],
    factual_tools_used: bool,
    sources: Iterable[dict[str, Any]] | None = None,
    profile: GroundingRepairShadowProfile | None = None,
) -> GroundingRepairShadowDecision:
    """Return the v2 decision while materializing source support only until first risk.

    The served answer is never changed by this function. Full semantic diagnostics remain
    available through ``evaluate_grounding_repair_shadow`` for offline analysis.
    """
    active_profile = profile or GroundingRepairShadowProfile()
    initial_codes = tuple(str(code) for code in initial_error_codes)
    candidate_codes = tuple(str(code) for code in candidate_error_codes)
    known_source_ids = {
        str(source_id).strip() for source_id in source_ids if str(source_id).strip()
    }
    before_lines, valid_before, _, _ = _answer_analysis(
        prepared_answer, known_source_ids, include_semantic_lines=False
    )
    after_lines, valid_after, candidate_citations, semantic_lines = _answer_analysis(
        candidate_answer, known_source_ids, include_semantic_lines=True
    )
    active_profile.semantic_line_count = len(semantic_lines)

    unknown_candidate_citations = tuple(sorted(candidate_citations - known_source_ids))
    retained_line_count = _retained_count(before_lines, after_lines)
    line_count = sum(before_lines.values())
    retention = retained_line_count / line_count if line_count else 1.0
    preserved_valid_count = _retained_count(valid_before, valid_after)
    valid_line_count = sum(valid_before.values())
    all_valid_preserved = preserved_valid_count == valid_line_count
    candidate_nonempty = bool(str(candidate_answer or "").strip())
    candidate_has_valid_citation = bool(candidate_citations & known_source_ids)
    candidate_has_marker = bool(
        _INTERNAL_TOOL_ARTIFACT_RE.search(candidate_answer or "")
    )

    common: dict[str, object] = {
        "evaluated": True,
        "would_skip_repair": False,
        "initial_error_codes": initial_codes,
        "candidate_error_codes": candidate_codes,
        "substantive_line_count": line_count,
        "retained_substantive_line_count": retained_line_count,
        "substantive_line_retention": retention,
        "valid_cited_line_count": valid_line_count,
        "preserved_valid_cited_line_count": preserved_valid_count,
        "all_valid_cited_lines_preserved": all_valid_preserved,
        "candidate_nonempty": candidate_nonempty,
        "candidate_has_valid_citation": candidate_has_valid_citation,
        "candidate_unknown_citations": unknown_candidate_citations,
        "candidate_has_internal_marker": candidate_has_marker,
        "semantic_risk_codes": (),
        "semantic_risk_line_count": 0,
        "policy_version": GROUNDING_REPAIR_SHADOW_POLICY_VERSION,
    }
    checks = (
        (not known_source_ids, "no_source_records"),
        (len(initial_codes) > 2, "too_many_initial_errors"),
        ("missing_citations" in initial_codes, "missing_citations"),
        ("unknown" in initial_codes, "unknown_error_taxonomy"),
        (
            not set(initial_codes).issubset(_ALLOWED_INITIAL_ERROR_CODES),
            "unsupported_error_signature",
        ),
        (bool(candidate_codes), "candidate_validation_failed"),
        (not candidate_nonempty, "candidate_empty"),
        (candidate_has_marker, "candidate_internal_marker"),
        (bool(unknown_candidate_citations), "candidate_unknown_citation"),
        (
            factual_tools_used
            and bool(known_source_ids)
            and not candidate_has_valid_citation,
            "candidate_missing_valid_citation",
        ),
        (not all_valid_preserved, "valid_cited_lines_not_preserved"),
        (
            retention < GROUNDING_REPAIR_SHADOW_MIN_RETENTION,
            "substantive_retention_below_threshold",
        ),
    )
    for failed, reason in checks:
        if failed:
            return _decision(reason, common)

    source_records = {
        str(source.get("source_id") or "").strip(): source
        for source in (sources or [])
        if isinstance(source, dict)
        and str(source.get("source_id") or "").strip()
    }
    if not source_records:
        active_profile.semantic_analysis_complete = True
        return _decision(
            "candidate_semantic_support_unverified",
            {
                **common,
                "semantic_risk_codes": ("semantic_source_records_unavailable",),
            },
        )

    source_support_cache: dict[str, GroundingSemanticSourceSupport] = {}
    for index, (citations, claim_text) in enumerate(semantic_lines):
        active_profile.semantic_lines_evaluated += 1
        line_risks: set[str] = set()
        if not citations:
            line_risks.add("uncited_semantic_content")
        elif citations.issubset(source_records):
            supports = [
                _support_for_source(
                    source_id, source_records, source_support_cache, active_profile
                )
                for source_id in citations
            ]
            claim_tokens = _semantic_tokens(claim_text)
            if len(supports) == 1:
                evidence_tokens = supports[0].tokens
                evidence_numbers = supports[0].numbers
                source_kinds = {supports[0].kind}
            else:
                evidence_tokens = set().union(*(item.tokens for item in supports))
                evidence_numbers = set().union(*(item.numbers for item in supports))
                source_kinds = {item.kind for item in supports}
            support = (
                len(claim_tokens & evidence_tokens) / len(claim_tokens)
                if claim_tokens
                else 1.0
            )
            if support < GROUNDING_REPAIR_MIN_CITATION_TOKEN_SUPPORT:
                line_risks.add("citation_semantic_support_low")
            claim_numbers = set(_NUMBER_RE.findall(claim_text))
            if not claim_numbers.issubset(evidence_numbers):
                line_risks.add("citation_numeric_support_mismatch")
            if (
                len(citations) == 1
                and source_kinds & {"portfolio", "portfolio_query"}
                and _SCOPE_AGGREGATION_RE.search(claim_text)
            ):
                line_risks.add("portfolio_scope_aggregation_unverified")
        if line_risks:
            active_profile.semantic_analysis_complete = index == len(semantic_lines) - 1
            active_profile.semantic_short_circuit = not active_profile.semantic_analysis_complete
            return _decision(
                "candidate_semantic_support_unverified",
                {
                    **common,
                    "semantic_risk_codes": tuple(sorted(line_risks)),
                    "semantic_risk_line_count": 1,
                },
            )

    active_profile.semantic_analysis_complete = True
    return _decision(
        "verified_partial_passed_policy", common, would_skip_repair=True
    )


__all__ = [
    "GroundingRepairShadowProfile",
    "evaluate_grounding_repair_shadow_fast",
]