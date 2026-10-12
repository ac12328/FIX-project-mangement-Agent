"""Deterministic typed rules for offline grounding-repair Policy-v5 research.

The serving runtime must not import this module. It exposes only deterministic
contracts and non-sensitive decision features to the certificate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Literal

from .grounding_repair_certificate_v5_projection import (
    AGGREGATE_SOURCE_KINDS,
    CURRENT_SOURCE_KINDS,
    DATE_RE,
    MILESTONE_RE,
    NUMBER_RE,
    PROJECT_ID_RE,
    QUERY_SOURCE_KINDS,
    RANKING_SOURCE_KINDS,
    READINESS_SOURCE_KINDS,
    ProjectionCacheV5,
    SourceProjectionV5,
    build_source_projections_v5,
    canonical_milestones,
    claim_tokens,
    normalize_date,
    normalized_dates,
    normalized_scalars,
)

POLICY_VERSION = "workspace-grounding-repair-certificate-v5"
MIN_SUBSTANTIVE_RETENTION = 0.80

CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
INTERNAL_MARKER_RE = re.compile(r"\bmonads0x[a-z0-9_]+\b", re.IGNORECASE)
OMISSION_NOTICE = (
    "> Some statements were omitted because the Workspace could not verify "
    "their source citations."
)
ALLOWED_INITIAL_ERROR_CODES = frozenset(
    {"uncited_factual_line", "unknown_citation", "internal_tool_marker"}
)

CURRENT_RE = re.compile(
    r"\b(?:current|currently|latest|as[- ]of|today|now)\b|"
    r"(?:\u76ee\u524d|\u73fe\u6642|\u73fe\u6cc1|\u7576\u524d|\u6700\u65b0|\u622a\u81f3|\u4eca\u5929|\u73fe\u5728)",
    re.IGNORECASE,
)
LATEST_RE = re.compile(
    r"\b(?:latest|newest|most recent)\b|(?:\u6700\u65b0|\u6700\u8fd1)",
    re.IGNORECASE,
)
THREAD_LATEST_MESSAGE_RE = re.compile(
    r"\b(?:latest|newest|most recent)\s+(?:message|email|reply)\b|"
    r"(?:\u6700\u65b0|\u6700\u8fd1)(?:\u4e00\u5c01)?(?:\u8a0a\u606f|\u90f5\u4ef6|\u56de\u8986)",
    re.IGNORECASE,
)
NO_NEWER_OUTBOUND_RE = re.compile(
    r"\bno\s+newer\s+outbound(?:\s+(?:message|email|reply))?\b|"
    r"(?:\u6c92\u6709|\u7121)(?:\u66f4\u65b0|\u4e4b\u5f8c)(?:\u7684)?(?:\u5916\u767c|\u56de\u8986)",
    re.IGNORECASE,
)
READINESS_ASSERTION_RE = re.compile(
    r"\b(?:is|are|was|were|looks?|appears?)\s+(?:production[- ]?)?ready\b|"
    r"\b(?:production[- ]readiness|ready\s+for\s+(?:production|go[- ]?live))\b|"
    r"(?:\u5df2\u5c31\u7dd2|\u6e96\u5099\u5c31\u7dd2|\u53ef\u4e0a\u7dda|\u53ef\u6295\u5165\u751f\u7522)",
    re.IGNORECASE,
)
PROJECT_COMPLETION_ASSERTION_RE = re.compile(
    r"\b(?:project|project\s+status|status)\s*(?:is|=|:)?\s*(?:completed|archived|cancelled)\b|"
    r"\b(?:has|was)\s+been\s+(?:completed|archived|cancelled)\b|"
    r"(?:\u9805\u76ee|\u5c08\u6848)(?:\u72c0\u614b)?(?:\u5df2)?(?:\u5b8c\u6210|\u5c01\u5b58|\u53d6\u6d88)",
    re.IGNORECASE,
)
COMPLETION_RE = re.compile(
    rf"(?:{READINESS_ASSERTION_RE.pattern})|(?:{PROJECT_COMPLETION_ASSERTION_RE.pattern})",
    re.IGNORECASE,
)
ACTUAL_GO_LIVE_RE = re.compile(
    r"\b(?:went|has\s+gone|is|became)\s+live\b|\bgo[- ]live\s+(?:completed|complete|successful)\b|"
    r"(?:\u5df2\u6b63\u5f0f\u4e0a\u7dda|\u5df2\u4e0a\u7dda|\u4e0a\u7dda(?:\u5df2)?(?:\u5b8c\u6210|\u6210\u529f))",
    re.IGNORECASE,
)
GO_LIVE_RE = ACTUAL_GO_LIVE_RE
RANKING_RE = re.compile(
    r"\b(?:rank(?:ed|ing)?|top|bottom|highest|lowest|best|worst|"
    r"higher\s+than|lower\s+than|more\s+than|less\s+than|fewer\s+than|"
    r"most\s+(?:urgent|overdue|inactive|important)|least\s+(?:urgent|overdue|inactive|important))\b|"
    r"(?:\u6392\u540d|\u6700\u9ad8|\u6700\u4f4e|\u6700\u56b4\u91cd|\u6700\u9700\u8981|\u6700\u512a\u5148|"
    r"\u6700\u5927|\u6700\u5c0f|\u8f03\u9ad8|\u8f03\u4f4e|\u6700\u8feb\u5207|\u512a\u5148\u8655\u7406)",
    re.IGNORECASE,
)
AGGREGATION_RE = re.compile(
    r"\b(?:total|overall|combined|across\s+(?:the\s+)?portfolio|portfolio[- ]wide|"
    r"aggregate|aggregation|sum|count\s+of|number\s+of|all\s+projects?|every\s+project)\b|"
    r"(?:\u7e3d\u6578|\u5408\u8a08|\u6574\u9ad4|\u8de8\u7d44\u5408|\u5168\u7d44\u5408|\u7d44\u5408\u5167|"
    r"\u6240\u6709\u9805\u76ee|\u5168\u90e8\u9805\u76ee|\u6240\u6709\u5c08\u6848|\u5168\u90e8\u5c08\u6848)",
    re.IGNORECASE,
)
UNIVERSAL_SCOPE_RE = re.compile(
    r"\b(?:all|every|entire|whole)\s+(?:recorded\s+)?(?:projects?|portfolio|work|actions?)\b|"
    r"(?:\u6240\u6709|\u5168\u90e8|\u6574\u500b|\u5168\u7d44\u5408|\u7d44\u5408\u5167|\u8de8\u7d44\u5408)"
    r"(?:\u9805\u76ee|\u5c08\u6848|\u7d44\u5408|\u5de5\u4f5c|\u884c\u52d5)?",
    re.IGNORECASE,
)
ACTIVE_SCOPE_RE = re.compile(
    r"\b(?:active|non[- ]?terminal|open|ongoing|in[- ]flight)\b|"
    r"(?:\u6d3b\u8e8d|\u975e\u7d42\u6b62|\u975e\u7d42\u6b62|\u958b\u653e|\u9032\u884c\u4e2d|\u672a\u5b8c\u6210)",
    re.IGNORECASE,
)
ADVISORY_RE = re.compile(
    r"^(?:recommendation|recommended\s+next\s+step|next\s+steps?|suggestion|"
    r"i\s+(?:recommend|suggest)|we\s+(?:recommend|suggest)|"
    r"(?:please\s+)?consider|you\s+(?:should|could|may)|operator[- ]attention|"
    r"\u5efa\u8b70|\u5efa\u8b70\u4e0b\u4e00\u6b65|\u4e0b\u4e00\u6b65|\u5efa\u8b70\u884c\u52d5|"
    r"\u53ef\u8003\u616e|\u61c9\u7576|\u61c9|\u512a\u5148|\u7acb\u5373|\u5148\u8655\u7406)",
    re.IGNORECASE,
)

_PROJECT_STATUS_VALUES = frozenset({"active", "blocked", "waiting", "completed", "archived", "cancelled"})
_OWNERSHIP_VALUES = frozenset({"internal", "external", "shared"})
_STATUS_VALUE_ALT = "active|blocked|waiting|completed|archived|cancelled"
_OWNERSHIP_VALUE_ALT = "internal|external|shared"
_EXPLICIT_STATUS_RE = re.compile(
    rf"\b(?:project[_ ]?status|status)\s*(?:is|=|:|\|)?\s*(?P<value>{_STATUS_VALUE_ALT})\b|"
    rf"\b(?:project|{PROJECT_ID_RE.pattern})\s+(?:is|remains?|became)\s+(?:currently\s+)?(?P<subject>{_STATUS_VALUE_ALT})\b(?!\s+(?:by|because|due\s+to|pending|awaiting))",
    re.IGNORECASE,
)
_EXPLICIT_OWNERSHIP_RE = re.compile(
    rf"\b(?:ball[_ ]?with|owner(?:ship)?|owned\s+by|waiting\s+on)\s*(?:is|=|:|\|)?\s*(?P<value>{_OWNERSHIP_VALUE_ALT})\b",
    re.IGNORECASE,
)
_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$")
_EXPLICIT_RANK_RE = re.compile(r"\brank(?:ed)?\s*(?:is|=|:|#)?\s*(?P<rank>\d+)\b", re.IGNORECASE)
_MARKDOWN_PREFIX_RE = re.compile(r"^(?:[-*+>]\s+|\d+[.)]\s+)")

LineKind = Literal["blank", "structural", "omission", "advisory", "factual"]
LineContractDecisionV5 = tuple[bool, str, tuple[str, ...]]
ProjectionContractKeyV5 = tuple[object, ...]


@dataclass(slots=True)
class LineContractCacheV5:
    """Plaintext-in-memory exact contract cache for one offline development run."""

    decisions: dict[
        tuple[str, tuple[ProjectionContractKeyV5, ...]],
        LineContractDecisionV5,
    ] = field(default_factory=dict)
    projection_keys: dict[int, ProjectionContractKeyV5] = field(default_factory=dict)

    def _projection_key(self, projection: SourceProjectionV5) -> ProjectionContractKeyV5:
        identity = id(projection)
        cached = self.projection_keys.get(identity)
        if cached is not None:
            return cached
        key: ProjectionContractKeyV5 = (
            projection.source_id,
            projection.kind,
            projection.text,
            projection.tokens,
            projection.scalars,
            projection.dates,
            projection.project_ids,
            projection.milestones,
            projection.project_statuses,
            projection.ownership_values,
            projection.temporal_values,
            projection.fields,
            projection.tools,
            tuple(sorted(projection.metadata.items())),
            projection.direct_scalars,
            projection.query_summary_scalars,
            projection.direct_tokens,
            projection.query_summary_tokens,
            projection.direct_fields,
            projection.query_summary_fields,
            projection.direct_temporal_values,
            projection.query_summary_temporal_values,
            projection.direct_text,
            projection.query_summary_text,
        )
        self.projection_keys[identity] = key
        return key

    def evaluate(
        self,
        *,
        question: str,
        claim: str,
        projections: Iterable[SourceProjectionV5],
        tool_results: Iterable[dict[str, Any]] = (),
        known_project_ids: frozenset[str] = frozenset(),
        all_projects_requested: bool = False,
    ) -> LineContractDecisionV5:
        cited = tuple(projections)
        key = (claim, tuple(self._projection_key(item) for item in cited))
        cached = self.decisions.get(key)
        if cached is not None:
            return cached
        decision = line_contract_v5(
            question=question,
            claim=claim,
            projections=cited,
            tool_results=tool_results,
            known_project_ids=known_project_ids,
            all_projects_requested=all_projects_requested,
        )
        self.decisions[key] = decision
        return decision


def strip_markdown_prefix(line: str) -> str:
    stripped = _MARKDOWN_PREFIX_RE.sub("", line.strip())
    return stripped.strip("*_ `")


def _table_enum_values(claim: str, allowed: frozenset[str]) -> set[str]:
    if "|" not in claim or not PROJECT_ID_RE.search(claim):
        return set()
    cells = [strip_markdown_prefix(item).casefold() for item in claim.split("|")]
    return {cell for cell in cells if cell in allowed}


def formal_project_statuses(claim: str) -> set[str]:
    output = {
        (match.group("value") or match.group("subject")).casefold()
        for match in _EXPLICIT_STATUS_RE.finditer(claim)
    }
    output.update(_table_enum_values(claim, _PROJECT_STATUS_VALUES))
    return output


def formal_ownership_values(claim: str) -> set[str]:
    output = {match.group("value").casefold() for match in _EXPLICIT_OWNERSHIP_RE.finditer(claim)}
    output.update(_table_enum_values(claim, _OWNERSHIP_VALUES))
    return output


def _has_specific_fact_signal(line: str) -> bool:
    return bool(
        CITATION_RE.search(line)
        or PROJECT_ID_RE.search(line)
        or MILESTONE_RE.search(line)
        or DATE_RE.search(line)
        or NUMBER_RE.search(line)
        or formal_project_statuses(line)
        or formal_ownership_values(line)
    )


def classify_line_v5(line: str) -> LineKind:
    stripped = line.strip()
    if not stripped:
        return "blank"
    if stripped == OMISSION_NOTICE:
        return "omission"
    if stripped.startswith("```") or _TABLE_SEPARATOR_RE.fullmatch(stripped):
        return "structural"
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) <= 120:
        return "structural"
    if CITATION_RE.search(stripped):
        return "factual"
    plain = strip_markdown_prefix(stripped.lstrip("#").strip())
    heading_like = stripped.startswith("#") or (
        stripped.startswith("**") and stripped.endswith("**")
    ) or (stripped.endswith(":") and len(stripped) <= 100)
    table_header = stripped.startswith("|") and stripped.endswith("|") and not _has_specific_fact_signal(stripped)
    if (heading_like or table_header) and not _has_specific_fact_signal(plain):
        return "structural"
    if ADVISORY_RE.search(plain) and not _has_specific_fact_signal(plain):
        return "advisory"
    return "factual"


def _query_lineage(projection: SourceProjectionV5) -> tuple[str, str] | None:
    tool = projection.query_tool
    if not tool:
        return None
    summary_id = projection.source_id if projection.is_query_summary else projection.query_summary_source_id
    if not summary_id:
        return None
    return tool, summary_id


def _homogeneous_query_lineage(
    projections: Iterable[SourceProjectionV5],
) -> tuple[str, str] | None:
    lineages = {_query_lineage(item) for item in projections}
    if None in lineages or len(lineages) != 1:
        return None
    return next(iter(lineages))


def _summary_sources(projections: Iterable[SourceProjectionV5]) -> tuple[SourceProjectionV5, ...]:
    return tuple(item for item in projections if item.is_query_summary)


def _row_sources(projections: Iterable[SourceProjectionV5]) -> tuple[SourceProjectionV5, ...]:
    return tuple(item for item in projections if not item.is_query_summary)


def _scope_contract(
    *,
    claim: str,
    projections: tuple[SourceProjectionV5, ...],
) -> bool:
    if not UNIVERSAL_SCOPE_RE.search(claim):
        return True
    summaries = _summary_sources(projections)
    if len(summaries) != 1:
        return False
    summary = summaries[0]
    if not summary.is_query or not summary.query_scope:
        return False
    if ACTIVE_SCOPE_RE.search(claim):
        return summary.query_scope in {"active_projects", "all_projects"}
    return summary.scope_all_projects and summary.query_scope == "all_projects"


def scope_contract_supported(
    *,
    question: str,
    claim: str,
    projections: Iterable[SourceProjectionV5],
    all_projects_requested: bool = False,
    tool_results: Iterable[dict[str, Any]] = (),
) -> bool:
    """Compatibility wrapper around the exact-citation scope contract."""
    del question, all_projects_requested, tool_results
    return _scope_contract(claim=claim, projections=tuple(projections))


def _ranking_contract(
    claim: str,
    projections: tuple[SourceProjectionV5, ...],
) -> bool:
    if not RANKING_RE.search(claim):
        return True
    rows = _row_sources(projections)
    if not rows or _homogeneous_query_lineage(projections) is None:
        return False
    if any(
        item.query_tool != "portfolio_attention"
        or item.query_ordering != "priority_desc"
        or "priority_score" not in item.direct_fields
        for item in rows
    ):
        return False
    lowered = claim.casefold()
    highest = bool(re.search(
        r"\b(?:top|highest|best|most\s+(?:urgent|important))\b|"
        r"(?:\u6700\u9ad8|\u6700\u9700\u8981|\u6700\u512a\u5148|"
        r"\u6700\u8feb\u5207|\u512a\u5148\u8655\u7406)",
        lowered,
    ))
    lowest = bool(re.search(
        r"\b(?:bottom|lowest|worst|least\s+(?:urgent|important))\b|"
        r"(?:\u6700\u4f4e)",
        lowered,
    ))
    if highest and (len(rows) != 1 or rows[0].query_rank != 1):
        return False
    if lowest:
        if len(rows) != 1 or rows[0].query_rank is None:
            return False
        row_count = rows[0].metadata.get("query_row_count")
        if not isinstance(row_count, (int, float)) or rows[0].query_rank != int(row_count):
            return False
    explicit_rank = _EXPLICIT_RANK_RE.search(claim)
    if explicit_rank:
        return (
            len(rows) == 1
            and rows[0].query_rank is not None
            and rows[0].query_rank == int(explicit_rank.group("rank"))
        )
    return highest or lowest


def _latest_contract(
    claim: str,
    projections: tuple[SourceProjectionV5, ...],
) -> bool:
    if not LATEST_RE.search(claim):
        return True
    summaries = _summary_sources(projections)
    rows = _row_sources(projections)
    if summaries:
        return len(summaries) == 1 and summaries[0].current_snapshot
    if rows and THREAD_LATEST_MESSAGE_RE.search(claim) and NO_NEWER_OUTBOUND_RE.search(claim):
        return all(
            item.query_tool == "list_unanswered_threads"
            and "candidate_state" in item.direct_fields
            and "latest_email_id" in item.direct_fields
            and "inbound_last_no_newer_outbound" in item.direct_text.casefold()
            and bool(item.direct_temporal_values)
            for item in rows
        )
    return bool(rows) and all(
        item.collection_latest
        and (
            item.query_ordering in {"occurred_desc", "created_desc"}
            or not item.query_ordering
        )
        for item in rows
    )


def _current_contract(
    claim: str,
    projections: tuple[SourceProjectionV5, ...],
) -> bool:
    if not CURRENT_RE.search(claim):
        return True
    if not all(item.kind in CURRENT_SOURCE_KINDS for item in projections):
        return False
    return all(
        item.current_snapshot
        or bool(item.direct_temporal_values)
        or (item.is_query_summary and bool(item.query_summary_temporal_values))
        for item in projections
    )


def _cross_project_contract(
    projections: tuple[SourceProjectionV5, ...],
) -> tuple[bool, set[str]]:
    rows = _row_sources(projections)
    if not rows or _homogeneous_query_lineage(rows) is None:
        return False, set()
    if any(not item.project_ids for item in rows):
        return False, set()
    project_ids = set().union(*(item.project_ids for item in rows))
    if len(project_ids) != len(rows) or len({item.source_id for item in rows}) != len(rows):
        return False, set()
    return True, {f"number:{len(rows)}", f"number:{len(project_ids)}"}


def _aggregation_contract(
    claim: str,
    projections: tuple[SourceProjectionV5, ...],
    claim_scalars: set[str],
) -> tuple[bool, set[str]]:
    if not AGGREGATION_RE.search(claim):
        return True, set()
    summaries = _summary_sources(projections)
    rows = _row_sources(projections)
    allowed: set[str] = set()
    if summaries:
        if len(summaries) != 1 or not summaries[0].is_query:
            return False, set()
        if rows and _homogeneous_query_lineage(projections) is None:
            return False, set()
        allowed.update(summaries[0].query_summary_scalars)
        allowed.update(*(item.direct_scalars for item in rows))
    else:
        valid_rows, derived = _cross_project_contract(projections)
        if not valid_rows:
            return False, set()
        allowed.update(derived)
        allowed.update(*(item.direct_scalars for item in rows))
    return claim_scalars.issubset(allowed), allowed


@lru_cache(maxsize=4096)
def _source_identity_patterns(
    source_id: str,
) -> tuple[tuple[tuple[re.Pattern[str], frozenset[str]], ...], tuple[re.Pattern[str], frozenset[str]] | None]:
    codes = re.findall(
        r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*(?![A-Za-z0-9])",
        source_id,
    )
    code_patterns = tuple(
        (
            re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            ),
            frozenset(claim_tokens(code)),
        )
        for code in codes
    )
    namespace_pattern = None
    if ":" in source_id:
        namespace = source_id.split(":", 1)[0]
        namespace_pattern = (
            re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(namespace)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            ),
            frozenset(claim_tokens(namespace)),
        )
    return code_patterns, namespace_pattern


def _claim_without_exact_source_codes(
    claim: str,
    projections: tuple[SourceProjectionV5, ...],
) -> tuple[str, set[str]]:
    """Remove only exact cited source-code components from scalar/material checks."""

    cleaned = claim
    matched_tokens: set[str] = set()
    for projection in projections:
        code_patterns, namespace_pattern = _source_identity_patterns(projection.source_id)
        matched_code = False
        for pattern, tokens in code_patterns:
            if pattern.search(cleaned):
                matched_code = True
                matched_tokens.update(tokens)
                cleaned = pattern.sub(" ", cleaned)
        if matched_code and namespace_pattern is not None:
            pattern, tokens = namespace_pattern
            if pattern.search(cleaned):
                matched_tokens.update(tokens)
                cleaned = pattern.sub(" ", cleaned)
    return cleaned, matched_tokens


def _scalar_contract(
    *,
    claim: str,
    claim_scalars: set[str],
    projections: tuple[SourceProjectionV5, ...],
) -> bool:
    if not claim_scalars:
        return True
    is_aggregate = bool(AGGREGATION_RE.search(claim))
    if is_aggregate:
        supported, _ = _aggregation_contract(claim, projections, claim_scalars)
        return supported
    allowed = set().union(*(item.direct_scalars for item in projections))
    summaries = _summary_sources(projections)
    rows = _row_sources(projections)
    if summaries:
        if not (CURRENT_RE.search(claim) or UNIVERSAL_SCOPE_RE.search(claim)):
            return False
        allowed.update(*(item.query_summary_scalars for item in summaries))
    if len(summaries) == 1 and not rows:
        summary = summaries[0]
        row_count = summary.metadata.get("query_row_count")
        distinct_project_count = summary.metadata.get("query_distinct_project_count")
        if isinstance(row_count, (int, float)):
            allowed.add(f"number:{int(row_count)}")
        if isinstance(distinct_project_count, (int, float)):
            allowed.add(f"number:{int(distinct_project_count)}")
    if rows and RANKING_RE.search(claim):
        for row in rows:
            if row.query_rank is not None:
                allowed.add(f"number:{row.query_rank}")
    return claim_scalars.issubset(allowed)


_TEMPLATE_TOKENS = frozenset({
    "workspace", "snapshot", "verified", "brief", "timeline", "activity", "email",
    "update", "recorded", "record", "project", "portfolio", "attention", "require",
    "required", "overall", "total", "combined", "summary", "detail", "next", "action",
    "owner", "owned", "ownership", "milestone", "priority", "score", "latest", "current",
    "currently", "date", "days", "day", "hours", "hour", "item", "case", "point",
    "question", "status", "source", "result", "count", "number", "selected", "active",
    "highest", "lowest", "top", "bottom", "best", "worst", "most", "least",
    "rank", "ranked", "ranking",
    "reached", "unanswered", "message", "email", "reply", "ready", "go", "live", "gone", "cited",
})


def _claim_material_tokens(value: str) -> set[str]:
    cleaned = re.sub(
        r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}(?:[T\s]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|UTC|HKT|[+-]\d{2}:?\d{2})?)?",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return claim_tokens(cleaned)


def _material_token_variants(token: str) -> frozenset[str]:
    """Return conservative English inflection variants for exact token comparison."""

    value = str(token).casefold()
    variants = {value}
    if not value.isascii() or not value.isalpha():
        return frozenset(variants)
    if len(value) > 5 and value.endswith("ied"):
        variants.add(value[:-3] + "y")
    if len(value) > 5 and value.endswith("ing"):
        stem = value[:-3]
        variants.update((stem, stem + "e"))
        if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
            variants.add(stem[:-1])
    if len(value) > 4 and value.endswith("ed"):
        stem = value[:-2]
        variants.update((stem, stem + "e"))
        if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
            variants.add(stem[:-1])
    return frozenset(item for item in variants if len(item) >= 3)


@lru_cache(maxsize=4096)
def _expanded_material_tokens(supported_tokens: frozenset[str]) -> frozenset[str]:
    """Precompute conservative variants once for each exact cited token set."""

    expanded: set[str] = set(supported_tokens)
    for supported in supported_tokens:
        expanded.update(_material_token_variants(supported))
    return frozenset(expanded)


def _material_token_supported(
    token: str,
    supported_tokens: frozenset[str],
    expanded_supported_tokens: frozenset[str] | None = None,
) -> bool:
    if token in supported_tokens:
        return True
    expanded = (
        expanded_supported_tokens
        if expanded_supported_tokens is not None
        else _expanded_material_tokens(supported_tokens)
    )
    return bool(_material_token_variants(token) & expanded)


def _material_contract(
    claim: str,
    projections: tuple[SourceProjectionV5, ...],
    *,
    source_identity_tokens: set[str],
    claim_has_scalars: bool,
) -> bool:
    summary_allowed = bool(
        AGGREGATION_RE.search(claim)
        or UNIVERSAL_SCOPE_RE.search(claim)
        or CURRENT_RE.search(claim)
    )
    supported_tokens = set().union(*(item.direct_tokens for item in projections))
    if summary_allowed:
        supported_tokens.update(*(
            item.query_summary_tokens for item in projections if item.is_query_summary
        ))
    frozen_supported_tokens = frozenset(supported_tokens)
    expanded_supported_tokens = _expanded_material_tokens(frozen_supported_tokens)
    claimed = _claim_material_tokens(claim) - _TEMPLATE_TOKENS - source_identity_tokens
    if claimed and not all(
        _material_token_supported(
            token,
            frozen_supported_tokens,
            expanded_supported_tokens,
        )
        for token in claimed
    ):
        return False
    typed = bool(
        PROJECT_ID_RE.search(claim)
        or MILESTONE_RE.search(claim)
        or claim_has_scalars
        or formal_project_statuses(claim)
        or formal_ownership_values(claim)
        or AGGREGATION_RE.search(claim)
        or RANKING_RE.search(claim)
        or CURRENT_RE.search(claim)
        or READINESS_ASSERTION_RE.search(claim)
        or PROJECT_COMPLETION_ASSERTION_RE.search(claim)
        or ACTUAL_GO_LIVE_RE.search(claim)
    )
    return typed or bool(claimed)


def line_contract_v5(
    *,
    question: str,
    claim: str,
    projections: Iterable[SourceProjectionV5],
    tool_results: Iterable[dict[str, Any]] = (),
    known_project_ids: frozenset[str] = frozenset(),
    all_projects_requested: bool = False,
) -> tuple[bool, str, tuple[str, ...]]:
    del question, tool_results, known_project_ids, all_projects_requested
    cited = tuple(projections)
    claim = strip_markdown_prefix(claim)
    if not cited:
        return False, "candidate_unknown_citation", ("unknown_citation",)

    claimed_project_ids = {
        match.group(0).upper().replace("_", "-") for match in PROJECT_ID_RE.finditer(claim)
    }
    supported_project_ids = set().union(*(item.project_ids for item in cited))
    if not claimed_project_ids.issubset(supported_project_ids):
        return False, "project_identity_support_mismatch", ("project_identity_mismatch",)
    if claimed_project_ids and all(item.is_query_summary for item in cited):
        return False, "project_identity_support_mismatch", ("summary_cannot_prove_project_identity",)

    supported_milestones = set().union(*(item.milestones for item in cited))
    if not canonical_milestones(claim).issubset(supported_milestones):
        return False, "milestone_support_mismatch", ("milestone_mismatch",)

    supported_project_statuses = set().union(*(item.project_statuses for item in cited))
    if not formal_project_statuses(claim).issubset(supported_project_statuses):
        return False, "project_status_support_mismatch", ("project_status_mismatch",)
    supported_ownership = set().union(*(item.ownership_values for item in cited))
    if not formal_ownership_values(claim).issubset(supported_ownership):
        return False, "ownership_support_mismatch", ("ownership_mismatch",)

    scalar_claim, source_identity_tokens = _claim_without_exact_source_codes(claim, cited)
    claim_scalars = normalized_scalars(scalar_claim)
    if not _scalar_contract(claim=claim, claim_scalars=claim_scalars, projections=cited):
        return False, "numeric_or_date_support_mismatch", ("normalized_scalar_mismatch",)
    if AGGREGATION_RE.search(claim):
        aggregate_ok, _ = _aggregation_contract(claim, cited, claim_scalars)
        if not aggregate_ok:
            return False, "aggregation_source_contract_failed", ("aggregation_contract",)
    if not _ranking_contract(claim, cited):
        return False, "ranking_source_contract_failed", ("ranking_contract",)
    if not _latest_contract(claim, cited):
        return False, "latest_claim_query_contract_failed", ("latest_contract",)
    if not _current_contract(claim, cited):
        return False, "current_state_source_contract_failed", ("current_state_temporal_contract",)
    if not _scope_contract(claim=claim, projections=cited):
        return False, "scope_contract_failed", ("scope_ambiguity",)

    kinds = {item.kind for item in cited}
    readiness_asserted = bool(READINESS_ASSERTION_RE.search(claim))
    completion_asserted = bool(PROJECT_COMPLETION_ASSERTION_RE.search(claim))
    if readiness_asserted:
        readiness_signal = bool(kinds & READINESS_SOURCE_KINDS) or any(
            str(item.metadata.get("assessment_status") or "") in {"ready", "not_ready", "needs_review"}
            for item in cited
        )
        if not readiness_signal:
            return False, "completion_readiness_source_contract_failed", ("completion_readiness_contract",)
    if completion_asserted and not (
        formal_project_statuses(claim) & supported_project_statuses & {"completed", "archived", "cancelled"}
    ):
        return False, "completion_readiness_source_contract_failed", ("completion_readiness_contract",)

    if ACTUAL_GO_LIVE_RE.search(claim) and not (
        "M8_GOLIVE" in supported_milestones
        or "completed" in supported_project_statuses
        or any("go-live" in item.direct_text.casefold() or "go live" in item.direct_text.casefold() for item in cited)
    ):
        return False, "go_live_support_contract_failed", ("go_live_contract",)

    if not _material_contract(
        claim,
        cited,
        source_identity_tokens=source_identity_tokens,
        claim_has_scalars=bool(claim_scalars),
    ):
        return False, "claim_material_support_mismatch", ("direct_material_mismatch",)
    return True, "typed_claim_supported", ()


__all__ = [
    "ACTIVE_SCOPE_RE", "ADVISORY_RE", "AGGREGATION_RE", "ALLOWED_INITIAL_ERROR_CODES",
    "CITATION_RE", "COMPLETION_RE", "CURRENT_RE", "INTERNAL_MARKER_RE",
    "MIN_SUBSTANTIVE_RETENTION", "OMISSION_NOTICE", "POLICY_VERSION", "PROJECT_ID_RE",
    "LineContractCacheV5", "ProjectionCacheV5", "RANKING_RE", "SourceProjectionV5", "build_source_projections_v5",
    "claim_tokens", "classify_line_v5", "formal_ownership_values", "formal_project_statuses",
    "line_contract_v5", "normalize_date", "normalized_dates", "normalized_scalars",
    "scope_contract_supported", "strip_markdown_prefix",
]
