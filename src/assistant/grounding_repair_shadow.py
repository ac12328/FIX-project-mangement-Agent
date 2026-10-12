"""Shadow-only policy for evaluating selective Workspace grounding repair."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping

GROUNDING_REPAIR_SHADOW_POLICY_V1 = "workspace-grounding-repair-shadow-v1"
GROUNDING_REPAIR_SHADOW_POLICY_VERSION = "workspace-grounding-repair-shadow-v2"
GROUNDING_REPAIR_SHADOW_MIN_RETENTION = 0.80
GROUNDING_REPAIR_MIN_CITATION_TOKEN_SUPPORT = 0.80

_CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
_INTERNAL_TOOL_ARTIFACT_RE = re.compile(r"\bmonads0x[a-z0-9_]+\b", re.IGNORECASE)
_ALLOWED_INITIAL_ERROR_CODES = frozenset({
    "uncited_factual_line",
    "unknown_citation",
    "internal_tool_marker",
})
_OMISSION_NOTICE_RE = re.compile(
    r"^>\s*Some statements were omitted because the Workspace could not verify "
    r"their source citations\.?$",
    re.IGNORECASE,
)
_SCOPE_AGGREGATION_RE = re.compile(
    r"\b(?:total|overall|combined|across|portfolio|aggregate|sum|count|number of|"
    r"all projects?|every project|only|exactly|most|least|higher|lower|top|bottom|"
    r"priority|urgent|attention)\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_:-]+")
_NUMBER_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d+(?:\.\d+)?%?\b")
_NORMALIZE_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
_SOURCE_JSON_ENCODER = json.JSONEncoder(
    ensure_ascii=False, sort_keys=False, default=str, separators=(",", ":")
)
_TOKEN_STOPWORDS = frozenset({
    "a", "about", "all", "an", "and", "answer", "are", "as", "at", "based",
    "be", "been", "being", "but", "by", "can", "could", "current", "currently",
    "did", "do", "does", "each", "for", "from", "had", "has", "have", "here",
    "if", "in", "include", "includes", "including", "into", "is", "it", "its",
    "latest", "may", "might", "no", "not", "of", "on", "or", "over", "per",
    "project", "projects", "record", "records", "said", "says", "show", "shows",
    "some", "source", "sources", "than", "that", "the", "their", "then", "there",
    "these", "this", "those", "to", "under", "was", "were", "will", "with",
    "without", "workspace", "would",
})

ShadowDecisionName = Literal[
    "would_skip_repair",
    "require_repair",
    "legacy_no_repair",
    "shadow_error",
]


@dataclass(frozen=True)
class GroundingSemanticSourceSupport:
    """In-memory semantic support derived from one encrypted source record."""

    tokens: frozenset[str]
    numbers: frozenset[str]
    kind: str


@dataclass(frozen=True)
class GroundingRepairShadowDecision:
    """Side-effect-free shadow decision that never changes the returned answer."""

    decision: ShadowDecisionName
    reason: str
    evaluated: bool
    would_skip_repair: bool
    initial_error_codes: tuple[str, ...] = ()
    candidate_error_codes: tuple[str, ...] = ()
    substantive_line_count: int = 0
    retained_substantive_line_count: int = 0
    substantive_line_retention: float = 1.0
    valid_cited_line_count: int = 0
    preserved_valid_cited_line_count: int = 0
    all_valid_cited_lines_preserved: bool = True
    candidate_nonempty: bool = False
    candidate_has_valid_citation: bool = False
    candidate_unknown_citations: tuple[str, ...] = ()
    candidate_has_internal_marker: bool = False
    semantic_risk_codes: tuple[str, ...] = ()
    semantic_risk_line_count: int = 0
    policy_version: str = GROUNDING_REPAIR_SHADOW_POLICY_VERSION

    def as_metadata(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "decision": self.decision,
            "reason": self.reason,
            "evaluated": self.evaluated,
            "would_skip_repair": self.would_skip_repair,
            "initial_error_codes": list(self.initial_error_codes),
            "candidate_error_codes": list(self.candidate_error_codes),
            "substantive_line_count": self.substantive_line_count,
            "retained_substantive_line_count": self.retained_substantive_line_count,
            "substantive_line_retention": round(self.substantive_line_retention, 6),
            "valid_cited_line_count": self.valid_cited_line_count,
            "preserved_valid_cited_line_count": self.preserved_valid_cited_line_count,
            "all_valid_cited_lines_preserved": self.all_valid_cited_lines_preserved,
            "candidate_nonempty": self.candidate_nonempty,
            "candidate_has_valid_citation": self.candidate_has_valid_citation,
            "candidate_unknown_citations": list(self.candidate_unknown_citations),
            "candidate_has_internal_marker": self.candidate_has_internal_marker,
            "semantic_risk_codes": list(self.semantic_risk_codes),
            "semantic_risk_line_count": self.semantic_risk_line_count,
        }


def _retained_count(before: Counter[str], after: Counter[str]) -> int:
    return sum((before & after).values())


def _serialize_source_record(source: dict[str, Any]) -> str:
    """Serialize source support without ordering or whitespace work irrelevant to sets."""
    return _SOURCE_JSON_ENCODER.encode(source)


def _semantic_tokens(value: Any) -> set[str]:
    serialized = (
        value if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    )
    unique_tokens = set(_TOKEN_RE.findall(serialized.casefold()))
    return {
        token
        for token in unique_tokens
        if len(token) >= 2
        and token not in _TOKEN_STOPWORDS
        and not token.startswith("source:")
    }


def build_grounding_semantic_source_support(
    sources: Iterable[dict[str, Any]] | None,
) -> dict[str, GroundingSemanticSourceSupport]:
    """Precompute decision-only source support without persisting derived tokens."""
    support: dict[str, GroundingSemanticSourceSupport] = {}
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "").strip()
        if not source_id:
            continue
        serialized = _serialize_source_record(source)
        support[source_id] = GroundingSemanticSourceSupport(
            tokens=frozenset(_semantic_tokens(serialized)),
            numbers=frozenset(_NUMBER_RE.findall(serialized)),
            kind=str(source.get("kind") or ""),
        )
    return support


def _line_is_structural(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith(("#", "|---", "```"))
        or bool(_OMISSION_NOTICE_RE.fullmatch(stripped))
        or (stripped.endswith(":") and len(stripped) <= 80)
    )


def _answer_analysis(
    text: str,
    known_source_ids: set[str],
    *,
    include_semantic_lines: bool,
) -> tuple[Counter[str], Counter[str], set[str], list[tuple[set[str], str]]]:
    """Parse answer lines once for retention, citation, and semantic checks."""
    normalized_lines: list[str] = []
    valid_cited_lines: list[str] = []
    all_citations: set[str] = set()
    semantic_lines: list[tuple[set[str], str]] = []
    for raw_line in str(text or "").splitlines():
        citations = set(_CITATION_RE.findall(raw_line))
        all_citations.update(citations)
        claim_text = _CITATION_RE.sub("", raw_line) if citations else raw_line
        stripped = raw_line.strip()
        if stripped and not stripped.startswith(("#", "|---", "```")):
            normalized = (
                _NORMALIZE_NON_WORD_RE.sub(" ", claim_text.strip())
                .casefold()
                .strip()
            )
            if normalized:
                normalized_lines.append(normalized)
                if citations and citations.issubset(known_source_ids):
                    valid_cited_lines.append(normalized)
        if include_semantic_lines and not _line_is_structural(raw_line):
            semantic_lines.append((citations, claim_text))
    return (
        Counter(normalized_lines),
        Counter(valid_cited_lines),
        all_citations,
        semantic_lines,
    )


def _semantic_risks_for_lines(
    semantic_lines: Iterable[tuple[set[str], str]],
    source_records: dict[str, dict[str, Any]],
    semantic_source_support: Mapping[str, GroundingSemanticSourceSupport] | None = None,
) -> tuple[tuple[str, ...], int]:
    source_support_cache = dict(semantic_source_support or {})

    def source_support(source_id: str) -> GroundingSemanticSourceSupport:
        cached = source_support_cache.get(source_id)
        if cached is not None:
            return cached
        source = source_records[source_id]
        serialized = _serialize_source_record(source)
        cached = GroundingSemanticSourceSupport(
            tokens=frozenset(_semantic_tokens(serialized)),
            numbers=frozenset(_NUMBER_RE.findall(serialized)),
            kind=str(source.get("kind") or ""),
        )
        source_support_cache[source_id] = cached
        return cached

    risk_codes: set[str] = set()
    risky_lines = 0
    for citations, claim_text in semantic_lines:
        line_risks: set[str] = set()
        if not citations:
            line_risks.add("uncited_semantic_content")
        elif citations.issubset(source_records):
            supports = [source_support(source_id) for source_id in citations]
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
                if claim_tokens else 1.0
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
            risky_lines += 1
            risk_codes.update(line_risks)
    return tuple(sorted(risk_codes)), risky_lines


def _semantic_risks(
    candidate_answer: str,
    sources: Iterable[dict[str, Any]] | None,
) -> tuple[tuple[str, ...], int]:
    source_records = {
        str(source.get("source_id") or "").strip(): source
        for source in (sources or [])
        if isinstance(source, dict) and str(source.get("source_id") or "").strip()
    }
    if not source_records:
        return ("semantic_source_records_unavailable",), 0
    semantic_lines = []
    for raw_line in str(candidate_answer or "").splitlines():
        if _line_is_structural(raw_line):
            continue
        citations = set(_CITATION_RE.findall(raw_line))
        claim_text = _CITATION_RE.sub("", raw_line) if citations else raw_line
        semantic_lines.append((citations, claim_text))
    return _semantic_risks_for_lines(semantic_lines, source_records)


def legacy_no_repair_decision() -> GroundingRepairShadowDecision:
    """Return the shadow control observation for a baseline no-repair answer."""
    return GroundingRepairShadowDecision(
        decision="legacy_no_repair",
        reason="legacy_validation_passed",
        evaluated=False,
        would_skip_repair=False,
        candidate_nonempty=True,
    )


def shadow_error_decision(
    initial_error_codes: Iterable[str],
) -> GroundingRepairShadowDecision:
    """Return a fail-open observation when shadow computation itself fails."""
    return GroundingRepairShadowDecision(
        decision="shadow_error",
        reason="shadow_evaluation_error",
        evaluated=False,
        would_skip_repair=False,
        initial_error_codes=tuple(initial_error_codes),
    )


def _evaluate_grounding_repair_shadow(
    *,
    prepared_answer: str,
    initial_error_codes: Iterable[str],
    candidate_answer: str,
    candidate_error_codes: Iterable[str],
    source_ids: Iterable[str],
    factual_tools_used: bool,
    sources: Iterable[dict[str, Any]] | None,
    semantic_source_support: Mapping[str, GroundingSemanticSourceSupport] | None,
    semantic_guard: bool,
    policy_version: str,
) -> GroundingRepairShadowDecision:
    initial_codes = tuple(str(code) for code in initial_error_codes)
    candidate_codes = tuple(str(code) for code in candidate_error_codes)
    known_source_ids = {
        str(source_id).strip() for source_id in source_ids if str(source_id).strip()
    }
    before_lines, valid_before, _, _ = _answer_analysis(
        prepared_answer, known_source_ids, include_semantic_lines=False
    )
    after_lines, valid_after, candidate_citations, semantic_lines = _answer_analysis(
        candidate_answer, known_source_ids, include_semantic_lines=semantic_guard
    )
    unknown_candidate_citations = tuple(sorted(candidate_citations - known_source_ids))
    retained_line_count = _retained_count(before_lines, after_lines)
    line_count = sum(before_lines.values())
    retention = retained_line_count / line_count if line_count else 1.0

    preserved_valid_count = _retained_count(valid_before, valid_after)
    valid_line_count = sum(valid_before.values())
    all_valid_preserved = preserved_valid_count == valid_line_count

    candidate_nonempty = bool(str(candidate_answer or "").strip())
    candidate_has_valid_citation = bool(candidate_citations & known_source_ids)
    candidate_has_marker = bool(_INTERNAL_TOOL_ARTIFACT_RE.search(candidate_answer or ""))
    if semantic_guard:
        source_records = {
            str(source.get("source_id") or "").strip(): source
            for source in (sources or [])
            if isinstance(source, dict)
            and str(source.get("source_id") or "").strip()
        }
        semantic_risk_codes, semantic_risk_lines = (
            _semantic_risks_for_lines(
                semantic_lines, source_records, semantic_source_support
            )
            if source_records
            else (("semantic_source_records_unavailable",), 0)
        )
    else:
        semantic_risk_codes, semantic_risk_lines = (), 0

    common = {
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
        "semantic_risk_codes": semantic_risk_codes,
        "semantic_risk_line_count": semantic_risk_lines,
        "policy_version": policy_version,
    }

    checks = (
        (not known_source_ids, "no_source_records"),
        (len(initial_codes) > 2, "too_many_initial_errors"),
        ("missing_citations" in initial_codes, "missing_citations"),
        ("unknown" in initial_codes, "unknown_error_taxonomy"),
        (not set(initial_codes).issubset(_ALLOWED_INITIAL_ERROR_CODES), "unsupported_error_signature"),
        (bool(candidate_codes), "candidate_validation_failed"),
        (not candidate_nonempty, "candidate_empty"),
        (candidate_has_marker, "candidate_internal_marker"),
        (bool(unknown_candidate_citations), "candidate_unknown_citation"),
        (
            factual_tools_used and bool(known_source_ids) and not candidate_has_valid_citation,
            "candidate_missing_valid_citation",
        ),
        (not all_valid_preserved, "valid_cited_lines_not_preserved"),
        (retention < GROUNDING_REPAIR_SHADOW_MIN_RETENTION, "substantive_retention_below_threshold"),
        (semantic_guard and bool(semantic_risk_codes), "candidate_semantic_support_unverified"),
    )
    for failed, reason in checks:
        if failed:
            return GroundingRepairShadowDecision(
                decision="require_repair", reason=reason, **common
            )
    return GroundingRepairShadowDecision(
        decision="would_skip_repair",
        reason="verified_partial_passed_policy",
        **{**common, "would_skip_repair": True},
    )


def evaluate_grounding_repair_shadow_v1(
    *,
    prepared_answer: str,
    initial_error_codes: Iterable[str],
    candidate_answer: str,
    candidate_error_codes: Iterable[str],
    source_ids: Iterable[str],
    factual_tools_used: bool,
) -> GroundingRepairShadowDecision:
    """Replay the frozen v1 syntax-only policy for historical evidence."""
    return _evaluate_grounding_repair_shadow(
        prepared_answer=prepared_answer,
        initial_error_codes=initial_error_codes,
        candidate_answer=candidate_answer,
        candidate_error_codes=candidate_error_codes,
        source_ids=source_ids,
        factual_tools_used=factual_tools_used,
        sources=None,
        semantic_source_support=None,
        semantic_guard=False,
        policy_version=GROUNDING_REPAIR_SHADOW_POLICY_V1,
    )


def evaluate_grounding_repair_shadow(
    *,
    prepared_answer: str,
    initial_error_codes: Iterable[str],
    candidate_answer: str,
    candidate_error_codes: Iterable[str],
    source_ids: Iterable[str],
    factual_tools_used: bool,
    sources: Iterable[dict[str, Any]] | None = None,
    semantic_source_support: Mapping[
        str, GroundingSemanticSourceSupport
    ] | None = None,
) -> GroundingRepairShadowDecision:
    """Evaluate the conservative v2 semantic-support policy without side effects."""
    return _evaluate_grounding_repair_shadow(
        prepared_answer=prepared_answer,
        initial_error_codes=initial_error_codes,
        candidate_answer=candidate_answer,
        candidate_error_codes=candidate_error_codes,
        source_ids=source_ids,
        factual_tools_used=factual_tools_used,
        sources=sources,
        semantic_source_support=semantic_source_support,
        semantic_guard=True,
        policy_version=GROUNDING_REPAIR_SHADOW_POLICY_VERSION,
    )


__all__ = [
    "GROUNDING_REPAIR_MIN_CITATION_TOKEN_SUPPORT",
    "GROUNDING_REPAIR_SHADOW_MIN_RETENTION",
    "GROUNDING_REPAIR_SHADOW_POLICY_V1",
    "GROUNDING_REPAIR_SHADOW_POLICY_VERSION",
    "GroundingRepairShadowDecision",
    "GroundingSemanticSourceSupport",
    "build_grounding_semantic_source_support",
    "evaluate_grounding_repair_shadow",
    "evaluate_grounding_repair_shadow_v1",
    "legacy_no_repair_decision",
    "shadow_error_decision",
]
