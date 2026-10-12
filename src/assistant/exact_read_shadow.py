"""Shadow-only evaluator for deterministic exact portfolio-count reads.

The evaluator never changes routing or the served Workspace answer. It parses a
small, conservative query subset, executes the existing read-only portfolio
tool with isolated sinks, and emits privacy-safe comparison metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from src.models.enums import Milestone
from src.observability.contracts import TraceContext
from src.observability.lifecycle import emit_event
from src.tools.assistant_workspace_handlers import workspace_query_portfolio

EXACT_READ_SHADOW_RENDERER_VERSION = "workspace-exact-read-shadow-v1"

_COUNT_INTENT_RE = re.compile(r"\b(?:how many|count|number of)\b", re.IGNORECASE)
_PROJECT_RE = re.compile(r"\b(?:projects?|portfolio)\b", re.IGNORECASE)
_GENERATIVE_RE = re.compile(
    r"\b(?:analy[sz]e|analysis|compare|contrast|draft|explain|recommend|suggest|"
    r"summari[sz]e|synthesis|write|why)\b|"
    r"\bwhat should (?:i|we)\b|\bhow should (?:i|we)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:today|yesterday|tomorrow|since|before|after|between|during|as of)\b|"
    r"\b(?:this|last|next)\s+(?:day|week|month|quarter|year)\b|"
    r"\bq[1-4]\b|\b20\d{2}\b",
    re.IGNORECASE,
)
_GROUPING_RE = re.compile(
    r"\b(?:group(?:ed)? by|breakdown|split by|per (?:status|market|milestone)|"
    r"each (?:status|market|milestone))\b",
    re.IGNORECASE,
)
_ALL_PROJECTS_RE = re.compile(
    r"\b(?:all (?:the )?projects?|every project|entire portfolio|full portfolio|whole portfolio)\b",
    re.IGNORECASE,
)
_NON_TERMINAL_RE = re.compile(r"\bnon[ -]?terminal projects?\b", re.IGNORECASE)
_TERMINAL_UNION_RE = re.compile(r"\bterminal projects?\b", re.IGNORECASE)
_CLIENT_FILTER_RE = re.compile(r"\b(?:client|customer|broker)\b", re.IGNORECASE)

_STATUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Active", re.compile(r"\bactive\b", re.IGNORECASE)),
    ("Blocked", re.compile(r"\bblocked\b", re.IGNORECASE)),
    ("Waiting", re.compile(r"\bwaiting\b", re.IGNORECASE)),
    ("Completed", re.compile(r"\bcompleted\b", re.IGNORECASE)),
    ("Archived", re.compile(r"\barchived\b", re.IGNORECASE)),
    ("Cancelled", re.compile(r"\bcancelled|canceled\b", re.IGNORECASE)),
)
_BALL_RE = re.compile(
    r"\bball(?:\s+is|\s+with)?\s+(internal|external|shared|unknown)\b|"
    r"\b(internal|external|shared|unknown)\s+ball\b",
    re.IGNORECASE,
)
_MILESTONE_RE = re.compile(r"\bm([1-8])(?=[^0-9]|$)", re.IGNORECASE)
_ANY_MILESTONE_RE = re.compile(r"\bm\d+\b", re.IGNORECASE)
_MARKET_RE = re.compile(
    r"\bmarket\s+(?:is\s+)?([a-z]{2,8})\b|"
    r"\bin\s+([a-z]{2,8})\s+market\b|"
    r"\b([a-z]{2,8})\s+market\b",
    re.IGNORECASE,
)
_MARKET_CODES = frozenset({"AU", "CN", "EU", "HK", "JP", "PM", "SG", "UK", "US"})
_TERMINAL_STATUSES = frozenset({"Completed", "Archived", "Cancelled"})


@dataclass(frozen=True)
class ExactReadShadowPlan:
    """Conservative deterministic-read eligibility decision."""

    eligible: bool
    reason: str
    query_kind: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    filter_kind: str | None = None
    all_projects_requested: bool = False
    renderer_version: str = EXACT_READ_SHADOW_RENDERER_VERSION

    def as_metadata(self) -> dict[str, Any]:
        """Return metadata that excludes the question and filter values."""
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "query_kind": self.query_kind,
            "tool_name": self.tool_name,
            "filter_kind": self.filter_kind,
            "all_projects_requested": self.all_projects_requested,
            "renderer_version": self.renderer_version,
            "argument_count": len(self.arguments),
        }


@dataclass(frozen=True)
class ExactReadShadowResult:
    """One isolated deterministic renderer execution result."""

    execution_status: str
    reason: str
    duration_ms: float = 0.0
    total: int | None = None
    source_count: int = 0
    candidate_text: str | None = None
    candidate_hash: str | None = None
    error_class: str | None = None
    deterministic_arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExactReadShadowComparison:
    """Numeric-only comparison; it is not a semantic quality judgment."""

    agent_numeric_values: tuple[int, ...]
    agent_count_assertions: tuple[int, ...]
    agent_mentions_expected_count: bool
    exact_count_match: bool
    conflicting_count_assertions: bool


_MILESTONE_BY_NUMBER = {index: item.value for index, item in enumerate(Milestone, start=1)}


def _ineligible(reason: str) -> ExactReadShadowPlan:
    return ExactReadShadowPlan(eligible=False, reason=reason, tool_name="query_portfolio")


def classify_exact_read_shadow(
    message: str,
    *,
    required_tools: Iterable[str] = (),
    scope_mode: str,
    web_enabled: bool,
) -> ExactReadShadowPlan:
    """Parse only unambiguous exact portfolio-count questions.

    This classifier is independent of production required-tool nudges so it can
    measure opportunities that the current router does not recognize.
    """
    text = " ".join(str(message or "").split())
    tools = tuple(sorted({str(item).strip() for item in required_tools if str(item).strip()}))
    if web_enabled:
        return _ineligible("web_enabled")
    if scope_mode not in {"portfolio", "projects"}:
        return _ineligible("unsupported_scope_mode")
    if not _COUNT_INTENT_RE.search(text) or not _PROJECT_RE.search(text):
        return _ineligible("not_exact_project_count")
    if _GENERATIVE_RE.search(text):
        return _ineligible("generative_synthesis_requested")
    if _DATE_RE.search(text):
        return _ineligible("date_qualified_count_unsupported")
    if _GROUPING_RE.search(text):
        return _ineligible("grouped_count_unsupported")
    if _CLIENT_FILTER_RE.search(text):
        return _ineligible("client_filter_unsupported")

    all_projects_requested = bool(_ALL_PROJECTS_RE.search(text))
    non_terminal_requested = bool(_NON_TERMINAL_RE.search(text))
    if all_projects_requested and non_terminal_requested:
        return _ineligible("conflicting_scope_qualifiers")

    status_values = {
        canonical for canonical, pattern in _STATUS_PATTERNS if pattern.search(text)
    }
    ball_values = {
        (match.group(1) or match.group(2)).title() for match in _BALL_RE.finditer(text)
    }
    milestone_numbers = {int(match.group(1)) for match in _MILESTONE_RE.finditer(text)}
    market_values = {
        value
        for match in _MARKET_RE.finditer(text)
        for raw in match.groups()
        if raw and (value := raw.upper()) in _MARKET_CODES
    }

    if len(status_values) > 1 or len(ball_values) > 1 or len(milestone_numbers) > 1 or len(market_values) > 1:
        return _ineligible("multiple_filter_values")
    if _ANY_MILESTONE_RE.search(text) and not milestone_numbers:
        return _ineligible("unsupported_milestone")
    if "market" in text.lower() and not market_values:
        return _ineligible("unsupported_market_filter")
    if _TERMINAL_UNION_RE.search(text) and not non_terminal_requested and not status_values:
        return _ineligible("terminal_status_union_unsupported")
    if tools and tools != ("query_portfolio",):
        return _ineligible("conflicting_primary_tool")

    filters: list[tuple[str, str]] = []
    if status_values:
        filters.append(("project_status", next(iter(status_values))))
    if ball_values:
        filters.append(("ball_with", next(iter(ball_values))))
    if milestone_numbers:
        filters.append(("current_milestone", _MILESTONE_BY_NUMBER[next(iter(milestone_numbers))]))
    if market_values:
        filters.append(("market", next(iter(market_values))))
    if len(filters) > 1:
        return _ineligible("multiple_filters")

    if non_terminal_requested and filters and filters[0][0] == "project_status":
        if filters[0][1] in _TERMINAL_STATUSES:
            return _ineligible("conflicting_scope_filter")

    arguments: dict[str, Any] = dict(filters)
    if all_projects_requested:
        arguments["include_terminal"] = True
    if not filters and not all_projects_requested and not non_terminal_requested:
        return _ineligible("ambiguous_project_scope")

    return ExactReadShadowPlan(
        eligible=True,
        reason="eligible_exact_portfolio_count",
        query_kind="portfolio_count",
        tool_name="query_portfolio",
        arguments=arguments,
        filter_kind=filters[0][0] if filters else ("all_projects" if all_projects_requested else "non_terminal"),
        all_projects_requested=all_projects_requested,
    )


def execute_exact_read_shadow(
    conn: sqlite3.Connection,
    plan: ExactReadShadowPlan,
    *,
    scope_mode: str,
    project_ids: list[str],
    as_of: datetime,
) -> ExactReadShadowResult:
    """Execute the existing deterministic read with isolated local sinks."""
    if not plan.eligible:
        return ExactReadShadowResult(execution_status="skipped", reason=plan.reason)

    started = time.perf_counter()
    local_sources: list[dict[str, Any]] = []
    deterministic_arguments = {
        **plan.arguments,
        "limit": 1,
        "_assistant_scope_mode": scope_mode,
        "_assistant_scope_project_ids": list(project_ids),
        "_assistant_source_sink": local_sources,
        "_assistant_as_of": as_of.isoformat(),
        "_assistant_all_projects_requested": plan.all_projects_requested,
    }
    try:
        raw_result = workspace_query_portfolio(conn, deterministic_arguments)
        if not isinstance(raw_result, dict) or raw_result.get("error"):
            raise RuntimeError("Deterministic portfolio query failed")
        total = int(raw_result["total"])
        if total < 0:
            raise ValueError("Deterministic portfolio count was negative")
        summary_source_id = str(raw_result.get("summary_source_id") or "").strip()
        candidate_text = _render_candidate(plan, total, as_of, summary_source_id)
        return ExactReadShadowResult(
            execution_status="completed",
            reason=plan.reason,
            duration_ms=(time.perf_counter() - started) * 1000,
            total=total,
            source_count=len(local_sources),
            candidate_text=candidate_text,
            candidate_hash=_stable_hash(candidate_text),
            deterministic_arguments={
                **plan.arguments,
                "scope_mode": scope_mode,
                "scope_project_ids": list(project_ids),
                "as_of": as_of.isoformat(),
                "all_projects_requested": plan.all_projects_requested,
            },
        )
    except Exception as exc:
        return ExactReadShadowResult(
            execution_status="failed",
            reason=plan.reason,
            duration_ms=(time.perf_counter() - started) * 1000,
            source_count=len(local_sources),
            error_class=type(exc).__name__,
            deterministic_arguments={
                **plan.arguments,
                "scope_mode": scope_mode,
                "scope_project_ids": list(project_ids),
                "as_of": as_of.isoformat(),
                "all_projects_requested": plan.all_projects_requested,
            },
        )


def compare_exact_read_answer(answer: str, expected_count: int | None) -> ExactReadShadowComparison:
    """Compare explicit English/Chinese count claims, not incidental numbers."""
    cleaned = re.sub(r"\[\[source:[^\]]+\]\]", "", str(answer or ""), flags=re.IGNORECASE)
    cleaned = re.sub(r"[*_`~]", "", cleaned)
    number = r"(\d{1,3}(?:,\d{3})*)"
    numeric_values = tuple(_parse_number(item) for item in re.findall(
        r"(?<![\w.])\d{1,3}(?:,\d{3})*(?![\w.])", cleaned
    ))
    strong_assertions: list[int] = []
    for pattern in (
        rf"\bthere\s+(?:is|are|were)\s+(?:exactly\s+)?{number}\b",
        rf"\b(?:matched|found|total(?:ed)?|count|number)\b[^\d\n]{{0,32}}{number}\b",
        rf"(?:\u5171|\u7e3d\u5171|\u603b\u5171|\u5408\u8a08|\u5408\u8ba1|\u5339\u914d|"
        rf"\u6bd4\u5c0d|\u6bd4\u5bf9|\u67e5\u8a62|\u67e5\u8be2|\u627e\u5230|"
        rf"\u5171\u6709)[^\d\n]{{0,32}}{number}\s*"
        rf"(?:\u500b|\u4e2a|\u9805|\u9879|\u7b46|\u7b14)?",
    ):
        strong_assertions.extend(
            _parse_number(item) for item in re.findall(pattern, cleaned, re.IGNORECASE)
        )
    project_assertions = [
        _parse_number(item)
        for pattern in (
            rf"(?<![\w.]){number}\s*(?:\u500b|\u4e2a)?\s*"
            r"(?:(?:active|blocked|waiting|completed|archived|cancelled|canceled|matching|visible|"
            r"non[ -]?terminal|terminal|external|internal|shared|unknown|hk|sg|uk|us|market)\s+){0,4}"
            r"(?:projects?|\u5c08\u6848|\u9879\u76ee|\u9805\u76ee)\b",
            rf"(?<![\w.]){number}\s*(?:\u500b|\u4e2a|\u9805|\u9879|\u7b46|\u7b14)"
            r"[^\d\n]{0,32}(?:\u5c08\u6848|\u9879\u76ee|\u9805\u76ee)",
            rf"(?:\u5c08\u6848|\u9879\u76ee|\u9805\u76ee)[^\d\n]{{0,32}}"
            rf"(?:\u5171|\u7e3d\u5171|\u603b\u5171|\u5408\u8a08|\u5408\u8ba1|\u5339\u914d|"
            rf"\u6bd4\u5c0d|\u6bd4\u5bf9|\u6578\u91cf|\u6570\u91cf|\u6578|\u6570|\u70ba|\u4e3a)"
            rf"[^\d\n]{{0,16}}{number}\s*(?:\u500b|\u4e2a|\u9805|\u9879|\u7b46|\u7b14)?",
        )
        for item in re.findall(pattern, cleaned, re.IGNORECASE)
    ]
    zero_claim = re.search(
        r"\b(?:no|zero|none)\b[^\n]{0,40}\b(?:projects?|records?|rows?|matches?|results?)\b|"
        r"\b(?:(?:could|can|did)\s+not|couldn\'t|can\'t|didn\'t)\s+"
        r"(?:find|locate|identify)\s+(?:any\s+)?[^\n]{0,24}(?:projects?|records?|matches?|results?)\b|"
        r"(?:\u6c92\u6709|\u6ca1\u6709|\u7121|\u65e0|\u672a\u6709|\u627e\u4e0d\u5230|"
        r"\u67e5\u7121|\u67e5\u65e0|\u672a\u67e5\u5230|\u6c92\u6709\u627e\u5230|\u6ca1\u6709\u627e\u5230|"
        r"\u672a\u627e\u5230|\u672a\u767c\u73fe|\u672a\u53d1\u73b0|\u4e0d\u5b58\u5728|"
        r"\u66ab\u7121|\u6682\u65e0|\u4e26\u7121|\u5e76\u65e0)[^\n]{0,40}"
        r"(?:\u5c08\u6848|\u9879\u76ee|\u9805\u76ee|\u7d00\u9304|\u8bb0\u5f55|"
        r"\u7d50\u679c|\u7ed3\u679c|\u5339\u914d|\u6bd4\u5c0d|\u6bd4\u5bf9)|"
        r"(?:\u5c08\u6848|\u9879\u76ee|\u9805\u76ee|\u7d00\u9304|\u8bb0\u5f55|"
        r"\u7d50\u679c|\u7ed3\u679c)[^\n]{0,30}"
        r"(?:\u6c92\u6709|\u6ca1\u6709|\u7121|\u65e0|\u672a\u6709|\u627e\u4e0d\u5230|"
        r"\u67e5\u7121|\u67e5\u65e0|\u672a\u67e5\u5230|\u6c92\u6709\u627e\u5230|\u6ca1\u6709\u627e\u5230|"
        r"\u672a\u627e\u5230|\u4e0d\u5b58\u5728|\u70ba\u96f6|\u4e3a\u96f6)",
        cleaned,
        re.IGNORECASE,
    )
    assertions = [*strong_assertions, *project_assertions]
    if zero_claim:
        assertions.append(0)
        strong_assertions.append(0)
    assertion_values = tuple(dict.fromkeys(assertions))
    exact_match = expected_count is not None and expected_count in assertion_values
    conflicting_assertions = exact_match and any(
        value != expected_count for value in strong_assertions
    )
    return ExactReadShadowComparison(
        agent_numeric_values=numeric_values,
        agent_count_assertions=assertion_values,
        agent_mentions_expected_count=exact_match,
        exact_count_match=exact_match,
        conflicting_count_assertions=conflicting_assertions,
    )

def observe_exact_read_shadow(
    conn: sqlite3.Connection,
    *,
    trace_context: TraceContext,
    plan: ExactReadShadowPlan,
    served_answer: str,
    scope_mode: str,
    project_ids: list[str],
    web_enabled: bool,
    routing_mode: str,
    as_of: datetime,
) -> ExactReadShadowResult:
    """Execute, compare, and emit one shadow event without changing the answer."""
    result = execute_exact_read_shadow(
        conn,
        plan,
        scope_mode=scope_mode,
        project_ids=project_ids,
        as_of=as_of,
    )
    comparison = compare_exact_read_answer(served_answer, result.total)
    metadata = {
        **plan.as_metadata(),
        "execution_status": result.execution_status,
        "duration_ms": round(result.duration_ms, 4),
        "total": result.total,
        "source_count": result.source_count,
        "candidate_hash": result.candidate_hash,
        "candidate_char_count": len(result.candidate_text or ""),
        "agent_answer_hash": _stable_hash(served_answer) if plan.eligible else None,
        "agent_answer_char_count": len(served_answer) if plan.eligible else 0,
        "agent_numeric_value_count": len(comparison.agent_numeric_values),
        "agent_count_assertion_count": len(comparison.agent_count_assertions),
        "agent_mentions_expected_count": comparison.agent_mentions_expected_count,
        "exact_count_match": comparison.exact_count_match,
        "conflicting_count_assertions": comparison.conflicting_count_assertions,
        "error_class": result.error_class,
        "scope_mode": scope_mode,
        "project_count": len(project_ids),
        "web_enabled": web_enabled,
        "routing_mode": routing_mode,
        "served_answer_unchanged": True,
    }
    payload = None
    if plan.eligible:
        payload = {
            "shadow_candidate": result.candidate_text,
            "served_agent_answer": served_answer,
            "deterministic_arguments": result.deterministic_arguments,
            "deterministic_result": {
                "total": result.total,
                "source_count": result.source_count,
            },
            "numeric_comparison": {
                "agent_numeric_values": comparison.agent_numeric_values,
                "agent_count_assertions": comparison.agent_count_assertions,
            },
        }
    emit_event(
        trace_context,
        "routing.exact_read.shadow",
        status={"completed": "ok", "failed": "error"}.get(result.execution_status, "skipped"),
        duration_ms=result.duration_ms,
        metadata=metadata,
        payload=payload,
    )
    return result


def _render_candidate(
    plan: ExactReadShadowPlan,
    total: int,
    as_of: datetime,
    summary_source_id: str,
) -> str:
    noun = "project" if total == 1 else "projects"
    if plan.all_projects_requested:
        scope = "the full visible Workspace scope"
    elif "project_status" in plan.arguments:
        scope = "the visible Workspace scope"
    else:
        scope = "the active non-terminal Workspace scope"

    filter_text = ""
    for key in ("project_status", "current_milestone", "ball_with", "market"):
        if key in plan.arguments:
            label = {
                "project_status": "status",
                "current_milestone": "milestone",
                "ball_with": "ball with",
                "market": "market",
            }[key]
            filter_text = f" filtered by {label} {plan.arguments[key]}"
            break
    citation = f" [[source:{summary_source_id}]]" if summary_source_id else ""
    return f"{total} {noun} matched {scope}{filter_text} as of {as_of.isoformat()}.{citation}"


def _parse_number(value: str) -> int:
    return int(value.replace(",", ""))


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EXACT_READ_SHADOW_RENDERER_VERSION",
    "ExactReadShadowComparison",
    "ExactReadShadowPlan",
    "ExactReadShadowResult",
    "classify_exact_read_shadow",
    "compare_exact_read_answer",
    "execute_exact_read_shadow",
    "observe_exact_read_shadow",
]
