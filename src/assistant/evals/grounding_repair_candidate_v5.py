"""Offline-only typed candidate filtering for grounding-repair Policy-v5 research.

This module removes candidate lines that the exact cited-source contract cannot
support. It is not imported by the serving grounding runtime and does not alter
production answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .grounding_repair_certificate_v5_projection import ProjectionCacheV5
from .grounding_repair_certificate_v5_rules import (
    CITATION_RE,
    INTERNAL_MARKER_RE,
    LineContractCacheV5,
    OMISSION_NOTICE,
    classify_line_v5,
    line_contract_v5,
)


@dataclass(frozen=True, slots=True)
class CandidateFilterResultV5:
    """Plaintext-in-memory result with aggregate-only diagnostic counts."""

    candidate_answer: str
    input_line_count: int
    retained_line_count: int
    supported_cited_line_count: int
    retained_advisory_line_count: int
    retained_structural_line_count: int
    omission_notice_count: int
    dropped_factual_line_count: int
    dropped_internal_line_count: int
    dropped_unknown_citation_line_count: int
    dropped_unsupported_citation_line_count: int

    def as_metadata(self) -> dict[str, int]:
        return {
            "input_line_count": self.input_line_count,
            "retained_line_count": self.retained_line_count,
            "supported_cited_line_count": self.supported_cited_line_count,
            "retained_advisory_line_count": self.retained_advisory_line_count,
            "retained_structural_line_count": self.retained_structural_line_count,
            "omission_notice_count": self.omission_notice_count,
            "dropped_factual_line_count": self.dropped_factual_line_count,
            "dropped_internal_line_count": self.dropped_internal_line_count,
            "dropped_unknown_citation_line_count": self.dropped_unknown_citation_line_count,
            "dropped_unsupported_citation_line_count": self.dropped_unsupported_citation_line_count,
        }


def filter_grounding_repair_candidate_v5(
    *,
    candidate_answer: str,
    question: str,
    sources: Iterable[dict[str, Any]],
    tool_results: Iterable[dict[str, Any]] = (),
    projection_cache: ProjectionCacheV5 | None = None,
    contract_cache: LineContractCacheV5 | None = None,
) -> CandidateFilterResultV5:
    """Retain only lines accepted by the exact Policy-v5 typed contract."""

    source_list = [item for item in sources if isinstance(item, dict)]
    tool_result_list = [item for item in tool_results if isinstance(item, dict)]
    cache = projection_cache or ProjectionCacheV5.build(source_list, tool_result_list)
    known_source_ids = frozenset(cache.records)
    known_project_ids = cache.known_project_ids
    input_lines = str(candidate_answer or "").splitlines()
    cited_source_ids = frozenset(CITATION_RE.findall(candidate_answer or ""))
    projections = cache.materialize(cited_source_ids)

    kept: list[str] = []
    supported_cited = advisory = structural = omission = 0
    dropped_factual = dropped_internal = dropped_unknown = dropped_unsupported = 0
    omission_seen = False

    for raw_line in input_lines:
        line = raw_line.strip()
        if INTERNAL_MARKER_RE.search(line):
            dropped_internal += 1
            continue
        line_kind = classify_line_v5(line)
        if line_kind == "blank":
            continue
        if line_kind == "structural":
            kept.append(raw_line.rstrip())
            structural += 1
            continue
        if line_kind == "omission":
            if not omission_seen:
                kept.append(OMISSION_NOTICE)
                omission += 1
                omission_seen = True
            continue
        if line_kind == "advisory":
            kept.append(raw_line.rstrip())
            advisory += 1
            continue

        citations = set(CITATION_RE.findall(line))
        if not citations:
            dropped_factual += 1
            continue
        if not citations.issubset(known_source_ids) or not citations.issubset(projections):
            dropped_factual += 1
            dropped_unknown += 1
            continue
        claim = CITATION_RE.sub("", line).strip()
        evaluator = contract_cache.evaluate if contract_cache is not None else line_contract_v5
        supported, _, _ = evaluator(
            question=str(question or ""),
            claim=claim,
            projections=(projections[source_id] for source_id in sorted(citations)),
            tool_results=tool_result_list,
            known_project_ids=known_project_ids,
            all_projects_requested=cache.all_projects_requested,
        )
        if not supported:
            dropped_factual += 1
            dropped_unsupported += 1
            continue
        kept.append(raw_line.rstrip())
        supported_cited += 1

    if dropped_factual or dropped_internal:
        if not omission_seen:
            kept.append(OMISSION_NOTICE)
            omission += 1
            omission_seen = True

    output = "\n".join(kept).strip()
    if not output:
        output = OMISSION_NOTICE
        omission = 1

    return CandidateFilterResultV5(
        candidate_answer=output,
        input_line_count=len(input_lines),
        retained_line_count=len(output.splitlines()),
        supported_cited_line_count=supported_cited,
        retained_advisory_line_count=advisory,
        retained_structural_line_count=structural,
        omission_notice_count=omission,
        dropped_factual_line_count=dropped_factual,
        dropped_internal_line_count=dropped_internal,
        dropped_unknown_citation_line_count=dropped_unknown,
        dropped_unsupported_citation_line_count=dropped_unsupported,
    )


__all__ = ["CandidateFilterResultV5", "filter_grounding_repair_candidate_v5"]
