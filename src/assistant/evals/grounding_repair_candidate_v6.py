"""Offline-only Policy-v6 answer-level coherence rules.

Policy-v6 reuses the proven Policy-v5 typed source projection but adds conservative
answer-level invariants. It is research-only and must never be imported by serving
runtime modules.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, fields
from typing import Any, Iterable, Literal

from .grounding_repair_certificate_v5_projection import ProjectionCacheV5, SourceProjectionV5
from .grounding_repair_certificate_v5_rules import (
    AGGREGATION_RE,
    CITATION_RE,
    CURRENT_RE,
    INTERNAL_MARKER_RE,
    LineContractCacheV5,
    OMISSION_NOTICE,
    RANKING_RE,
    classify_line_v5,
    line_contract_v5,
    strip_markdown_prefix,
)

POLICY_VERSION_V6 = "workspace-grounding-repair-certificate-v6"
MIN_SECTION_FACTUAL_LINES = 1
MAX_FACTUAL_LINES_PER_SECTION = 8
MAX_ANSWER_FACTUAL_LINES = 20

_SCOPE_SUMMARY_RE = re.compile(
    r"\b(?:portfolio|projects?|work|items?|actions?|threads?|emails?|messages?|"
    r"overall|summary|attention|priorit|highest|lowest|top|all|active)\b|"
    r"(?:\u7d44\u5408|\u9805\u76ee|\u5c08\u6848|\u5de5\u4f5c|\u884c\u52d5|\u90f5\u4ef6|\u8a0a\u606f|"
    r"\u6458\u8981|\u6574\u9ad4|\u512a\u5148|\u6700\u9ad8|\u6700\u4f4e|\u6240\u6709|\u6d3b\u8e8d|\u9032\u884c\u4e2d)",
    re.IGNORECASE,
)
_ACTIVE_COLLECTION_SCOPE_RE = re.compile(
    r"\b(?:active|open|ongoing|in[- ]flight|non[- ]terminal)\s+"
    r"(?:portfolio|projects?|work|items?|actions?|threads?|emails?|messages?)\b|"
    r"(?:\u6d3b\u8e8d|\u9032\u884c\u4e2d|\u672a\u5b8c\u6210|\u975e\u7d42\u6b62)"
    r"(?:\u9805\u76ee|\u5c08\u6848|\u7d44\u5408|\u5de5\u4f5c|\u884c\u52d5|\u90f5\u4ef6|\u8a0a\u606f)",
    re.IGNORECASE,
)
_UNIVERSAL_COLLECTION_SCOPE_RE = re.compile(
    r"\b(?:all|every|entire|whole)\s+(?:recorded\s+)?"
    r"(?:portfolio|projects?|work|items?|actions?|threads?|emails?|messages?)\b|"
    r"(?:\u6240\u6709|\u5168\u90e8|\u6574\u500b)"
    r"(?:\u9805\u76ee|\u5c08\u6848|\u7d44\u5408|\u5de5\u4f5c|\u884c\u52d5|\u90f5\u4ef6|\u8a0a\u606f)",
    re.IGNORECASE,
)
_SELECTED_COLLECTION_SCOPE_RE = re.compile(
    r"\b(?:selected|specified|requested|these|those)\s+"
    r"(?:projects?|work|items?|actions?|threads?|emails?|messages?)\b|"
    r"(?:\u5df2\u9078|\u6307\u5b9a|\u6240\u9078|\u9019\u4e9b|\u90a3\u4e9b)"
    r"(?:\u9805\u76ee|\u5c08\u6848|\u5de5\u4f5c|\u884c\u52d5|\u90f5\u4ef6|\u8a0a\u606f)",
    re.IGNORECASE,
)
_SECTION_START_RE = re.compile(r"^(?:#{1,6}\s+|\*\*[^*]+\*\*\s*$)")
_LIST_PREFIX_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")
_TABLE_SEPARATOR_RE = re.compile(r"^\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")

AnswerDecisionNameV6 = Literal["accepted", "rejected"]


@dataclass(frozen=True, slots=True)
class CandidateFilterResultV6:
    candidate_answer: str
    input_line_count: int
    retained_line_count: int
    retained_section_count: int
    dropped_section_count: int
    supported_cited_line_count: int
    retained_structural_line_count: int
    omission_notice_count: int
    dropped_factual_line_count: int
    dropped_internal_line_count: int
    dropped_unknown_citation_line_count: int
    dropped_unsupported_citation_line_count: int
    dropped_summary_cited_line_count: int
    dropped_mixed_citation_line_count: int
    dropped_scope_mismatch_line_count: int
    dropped_orphan_structural_line_count: int

    def as_metadata(self) -> dict[str, int]:
        return {
            item.name: int(getattr(self, item.name))
            for item in fields(self)
            if item.name != "candidate_answer"
        }


@dataclass(frozen=True, slots=True)
class AnswerContractDecisionV6:
    decision: AnswerDecisionNameV6
    reason: str
    risk_codes: tuple[str, ...]
    factual_line_count: int
    section_count: int
    query_summary_line_count: int
    direct_source_line_count: int
    mixed_citation_line_count: int

    @property
    def accepted(self) -> bool:
        return self.decision == "accepted"


@dataclass(slots=True)
class _SectionV6:
    heading: str | None
    lines: list[str]


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    return bool(_SECTION_START_RE.match(stripped)) or (
        stripped.endswith(":")
        and len(stripped) <= 100
        and classify_line_v5(stripped) == "structural"
    )


def _is_table_header(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and not _TABLE_SEPARATOR_RE.fullmatch(stripped)


def _sections(lines: Iterable[str]) -> list[_SectionV6]:
    sections: list[_SectionV6] = []
    current = _SectionV6(heading=None, lines=[])
    for raw in lines:
        stripped = raw.strip()
        if _is_heading(stripped):
            if current.lines or current.heading is not None:
                sections.append(current)
            current = _SectionV6(heading=raw.rstrip(), lines=[])
        else:
            current.lines.append(raw.rstrip())
    if current.lines or current.heading is not None:
        sections.append(current)
    return sections


def _citation_roles(
    citations: set[str],
    projections: dict[str, SourceProjectionV5],
) -> tuple[bool, bool]:
    cited = [projections[item] for item in sorted(citations) if item in projections]
    return any(item.is_query_summary for item in cited), any(not item.is_query_summary for item in cited)


def _query_scopes(
    citations: set[str],
    projections: dict[str, SourceProjectionV5],
) -> set[str]:
    return {
        projection.query_scope
        for source_id in citations
        if (projection := projections.get(source_id)) is not None and projection.query_scope
    }


def _scope_language_supported(
    claim: str,
    scopes: set[str],
    *,
    require_collection_qualifier: bool = False,
) -> bool:
    active_scope = bool(_ACTIVE_COLLECTION_SCOPE_RE.search(claim))
    universal_scope = bool(_UNIVERSAL_COLLECTION_SCOPE_RE.search(claim))
    selected_scope = bool(_SELECTED_COLLECTION_SCOPE_RE.search(claim))
    if not scopes:
        return not (active_scope or universal_scope or selected_scope)
    if len(scopes) > 1:
        return False
    scope = next(iter(scopes))
    if active_scope and universal_scope:
        return scope == "active_projects"
    if universal_scope:
        return scope == "all_projects"
    if active_scope:
        return scope == "active_projects"
    if selected_scope:
        return scope == "selected_projects"
    if require_collection_qualifier and scope in {
        "active_projects", "all_projects", "selected_projects"
    }:
        return False
    return True


def _query_summary_claim_supported(
    claim: str,
    projections: tuple[SourceProjectionV5, ...],
) -> bool:
    if len(projections) != 1 or not projections[0].is_query_summary:
        return False
    summary = projections[0]
    if not _SCOPE_SUMMARY_RE.search(claim):
        return False
    if not (
        AGGREGATION_RE.search(claim)
        or RANKING_RE.search(claim)
        or CURRENT_RE.search(claim)
        or _ACTIVE_COLLECTION_SCOPE_RE.search(claim)
        or _UNIVERSAL_COLLECTION_SCOPE_RE.search(claim)
    ):
        return False
    return _scope_language_supported(
        claim,
        {summary.query_scope} if summary.query_scope else set(),
        require_collection_qualifier=True,
    )


def answer_contract_v6(
    *,
    candidate_answer: str,
    projection_cache: ProjectionCacheV5,
) -> AnswerContractDecisionV6:
    """Certify self-contained answer scope after transformation."""

    lines = [line for line in str(candidate_answer or "").splitlines() if line.strip()]
    cited_ids = set(CITATION_RE.findall(candidate_answer or ""))
    projections = projection_cache.materialize(cited_ids)
    factual = summary = direct = mixed = 0
    answer_scopes: set[str] = set()
    section_count = 0

    for section in _sections(lines):
        section_factual = 0
        section_scopes: set[str] = set()
        for line in section.lines:
            if classify_line_v5(line.strip()) != "factual":
                continue
            section_factual += 1
            factual += 1
            citations = set(CITATION_RE.findall(line))
            has_summary, has_direct = _citation_roles(citations, projections)
            if has_summary and has_direct:
                mixed += 1
            elif has_summary:
                summary += 1
            elif has_direct:
                direct += 1
            scopes = _query_scopes(citations, projections)
            section_scopes.update(scopes)
            answer_scopes.update(scopes)
        if section_factual:
            section_count += 1
            if section_factual > MAX_FACTUAL_LINES_PER_SECTION:
                return AnswerContractDecisionV6(
                    "rejected", "section_factual_budget_exceeded", ("answer_density_risk",),
                    factual, section_count, summary, direct, mixed,
                )
            if len(section_scopes) > 1:
                return AnswerContractDecisionV6(
                    "rejected", "section_mixed_query_scope", ("section_scope_ambiguous",),
                    factual, section_count, summary, direct, mixed,
                )
            if section.heading and _SCOPE_SUMMARY_RE.search(strip_markdown_prefix(section.heading)):
                if not _scope_language_supported(
                    section.heading,
                    section_scopes,
                    require_collection_qualifier=False,
                ):
                    return AnswerContractDecisionV6(
                        "rejected", "section_heading_scope_mismatch", ("section_scope_ambiguous",),
                        factual, section_count, summary, direct, mixed,
                    )

    if factual == 0:
        return AnswerContractDecisionV6(
            "rejected", "candidate_missing_factual_rows", ("candidate_not_reconstructable",),
            factual, section_count, summary, direct, mixed,
        )
    if factual > MAX_ANSWER_FACTUAL_LINES:
        return AnswerContractDecisionV6(
            "rejected", "answer_factual_budget_exceeded", ("answer_density_risk",),
            factual, section_count, summary, direct, mixed,
        )
    if mixed:
        return AnswerContractDecisionV6(
            "rejected", "mixed_summary_and_direct_citations", ("citation_role_ambiguous",),
            factual, section_count, summary, direct, mixed,
        )
    if len(answer_scopes) > 1:
        return AnswerContractDecisionV6(
            "rejected", "answer_mixed_query_scope", ("answer_scope_ambiguous",),
            factual, section_count, summary, direct, mixed,
        )
    return AnswerContractDecisionV6(
        "accepted", "answer_contract_passed_policy_v6", (),
        factual, section_count, summary, direct, mixed,
    )


def filter_grounding_repair_candidate_v6(
    *,
    candidate_answer: str,
    question: str,
    sources: Iterable[dict[str, Any]],
    tool_results: Iterable[dict[str, Any]] = (),
    projection_cache: ProjectionCacheV5 | None = None,
    contract_cache: LineContractCacheV5 | None = None,
) -> CandidateFilterResultV6:
    """Build a compact, section-coherent answer from independently supported rows."""

    source_list = [item for item in sources if isinstance(item, dict)]
    tool_result_list = [item for item in tool_results if isinstance(item, dict)]
    cache = projection_cache or ProjectionCacheV5.build(source_list, tool_result_list)
    input_lines = str(candidate_answer or "").splitlines()
    projections = cache.materialize(set(CITATION_RE.findall(candidate_answer or "")))
    known_source_ids = frozenset(cache.records)
    evaluator = contract_cache.evaluate if contract_cache is not None else line_contract_v5

    retained_sections: list[list[str]] = []
    dropped_sections = 0
    supported = structural = dropped_factual = dropped_internal = dropped_unknown = 0
    dropped_unsupported = dropped_summary = dropped_mixed = dropped_scope = dropped_orphan = 0

    for section in _sections(input_lines):
        retained_facts: list[str] = []
        section_scopes: set[str] = set()
        for raw_line in section.lines:
            line = raw_line.strip()
            if not line:
                continue
            if INTERNAL_MARKER_RE.search(line):
                dropped_internal += 1
                continue
            if classify_line_v5(line) != "factual":
                continue
            citations = set(CITATION_RE.findall(line))
            if not citations:
                dropped_factual += 1
                continue
            if not citations.issubset(known_source_ids) or not citations.issubset(projections):
                dropped_factual += 1
                dropped_unknown += 1
                continue
            cited = tuple(projections[source_id] for source_id in sorted(citations))
            has_summary = any(item.is_query_summary for item in cited)
            has_direct = any(not item.is_query_summary for item in cited)
            claim = CITATION_RE.sub("", line).strip()
            if has_summary and has_direct:
                dropped_factual += 1
                dropped_mixed += 1
                continue
            if has_summary and not _query_summary_claim_supported(claim, cited):
                dropped_factual += 1
                dropped_summary += 1
                continue
            scopes = _query_scopes(citations, projections)
            if not _scope_language_supported(claim, scopes):
                dropped_factual += 1
                dropped_scope += 1
                continue
            ok, _, _ = evaluator(
                question=str(question or ""),
                claim=claim,
                projections=cited,
                tool_results=tool_result_list,
                known_project_ids=cache.known_project_ids,
                all_projects_requested=cache.all_projects_requested,
            )
            if not ok:
                dropped_factual += 1
                dropped_unsupported += 1
                continue
            retained_facts.append(raw_line.rstrip())
            section_scopes.update(scopes)
        if len(section_scopes) > 1 or len(retained_facts) > MAX_FACTUAL_LINES_PER_SECTION:
            dropped_factual += len(retained_facts)
            dropped_scope += len(retained_facts)
            dropped_sections += 1
            continue
        if not retained_facts:
            if section.heading or any(line.strip() for line in section.lines):
                dropped_sections += 1
            continue
        output_section: list[str] = []
        if section.heading:
            if _SCOPE_SUMMARY_RE.search(strip_markdown_prefix(section.heading)) and not _scope_language_supported(
                section.heading,
                section_scopes,
                require_collection_qualifier=False,
            ):
                dropped_factual += len(retained_facts)
                dropped_scope += len(retained_facts)
                dropped_sections += 1
                continue
            output_section.append(section.heading)
            structural += 1
        table_header = next((line for line in section.lines if _is_table_header(line)), None)
        if table_header and all(line.strip().startswith("|") for line in retained_facts):
            output_section.append(table_header)
            separator = next((line for line in section.lines if _TABLE_SEPARATOR_RE.fullmatch(line.strip())), None)
            if separator:
                output_section.append(separator)
            structural += len(output_section) - int(bool(section.heading))
        elif table_header:
            dropped_orphan += 1
        output_section.extend(retained_facts)
        supported += len(retained_facts)
        retained_sections.append(output_section)

    kept = [line for section in retained_sections for line in section]
    if supported > MAX_ANSWER_FACTUAL_LINES:
        kept = []
        supported = 0
        dropped_scope += 1
        dropped_sections += len(retained_sections)
        retained_sections = []
    omission = 0
    if dropped_factual or dropped_internal or dropped_sections or not kept:
        kept.append(OMISSION_NOTICE)
        omission = 1
    output = "\n".join(kept).strip() or OMISSION_NOTICE
    return CandidateFilterResultV6(
        candidate_answer=output,
        input_line_count=len(input_lines),
        retained_line_count=len(output.splitlines()),
        retained_section_count=len(retained_sections),
        dropped_section_count=dropped_sections,
        supported_cited_line_count=supported,
        retained_structural_line_count=structural,
        omission_notice_count=omission,
        dropped_factual_line_count=dropped_factual,
        dropped_internal_line_count=dropped_internal,
        dropped_unknown_citation_line_count=dropped_unknown,
        dropped_unsupported_citation_line_count=dropped_unsupported,
        dropped_summary_cited_line_count=dropped_summary,
        dropped_mixed_citation_line_count=dropped_mixed,
        dropped_scope_mismatch_line_count=dropped_scope,
        dropped_orphan_structural_line_count=dropped_orphan,
    )


__all__ = [
    "AnswerContractDecisionV6", "CandidateFilterResultV6", "MAX_ANSWER_FACTUAL_LINES",
    "MAX_FACTUAL_LINES_PER_SECTION", "POLICY_VERSION_V6", "answer_contract_v6",
    "filter_grounding_repair_candidate_v6",
]
