"""
Aggregate Query (Blueprint Section 31 / Plan Step 3.10).
/query free text -> structured filters -> ranked results.
READ ONLY -> never modifies project truth.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.models.enums import BallWith, Milestone, ProjectStatus
from src.portfolio.query_builder import FILTERABLE_FIELDS, QueryResult, _normalize_filters, query_projects


@dataclass
class QueryDateRange:
    from_date: str | None = None
    to_date: str | None = None


@dataclass
class QueryScope:
    project_filter: str | None = None
    date_range: QueryDateRange = field(default_factory=QueryDateRange)
    contact_filter: str | None = None


@dataclass
class QueryContextProfile:
    task_type: str
    query_text: str
    generated_at: str
    scope: QueryScope = field(default_factory=QueryScope)
    fields_requested: list[str] = field(default_factory=list)
    sort_order: str | None = None
    limit: int | None = 20
    confidence: float = 0.0
    escalate_to_operator: bool = False
    escalation_reason: str | None = None


@dataclass
class AggregateQueryEvaluation:
    status: str
    profile: QueryContextProfile
    filters: dict = field(default_factory=dict)
    search_text: str | None = None
    clarification_question: str | None = None
    suggested_filters: dict = field(default_factory=dict)
    result: QueryResult | None = None


_TOKEN_RE = re.compile(r"[a-z0-9_]+")

_BALL_WITH_KEYWORDS = {
    "internal": BallWith.INTERNAL.value,
    "external": BallWith.EXTERNAL.value,
    "shared": BallWith.SHARED.value,
    "unknown": BallWith.UNKNOWN.value,
}

_STATUS_KEYWORDS = {
    "active": ProjectStatus.ACTIVE.value,
    "blocked": ProjectStatus.BLOCKED.value,
    "waiting": ProjectStatus.WAITING.value,
    "completed": ProjectStatus.COMPLETED.value,
    "archived": ProjectStatus.ARCHIVED.value,
    "cancelled": ProjectStatus.CANCELLED.value,
}

_MILESTONE_KEYWORDS = {
    "m1": Milestone.M1_NETWORK_SETUP.value,
    "m2": Milestone.M2_UAT_SESSION_CONFIGURATION.value,
    "m3": Milestone.M3_UAT_ENVIRONMENT_ACCESS.value,
    "m4": Milestone.M4_APPLICATION_TESTING.value,
    "m5": Milestone.M5_CONFORMANCE_TEST_CASES.value,
    "m6": Milestone.M6_TEST_REPORT_AND_PRODUCTION_PREPARATION.value,
    "m7": Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY.value,
    "m8": Milestone.M8_GO_LIVE.value,
}

_STATUS_QUERY_HINTS = (
    "status",
    "progress",
    "doing",
    "where are we",
    "where do we stand",
    "latest update",
    "狀態",
    "状态",
    "進度",
    "进度",
    "情況",
    "情况",
)

_PORTFOLIO_HINTS = (
    "all",
    "projects",
    "portfolio",
    "list",
    "show",
    "which",
    "scan",
    "overview",
)

_ADVANCED_QUERY_HINTS = {
    "ranking_or_scoring": (
        "top client",
        "top clients",
        "top 10",
        "rank",
        "ranking",
        "urgency",
        "urgency score",
        "by urgency",
        "priority score",
    ),
    "date_or_age_window": (
        "this quarter",
        "last quarter",
        "this month",
        "last month",
        "this week",
        "last week",
        "older than",
        "days old",
        "days overdue",
        "overdue",
        "aged",
        "aging",
    ),
}


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(_normalize_text(text)))


def _heuristic_parse_query(free_text: str) -> tuple[dict, str | None]:
    text = _normalize_text(free_text)
    tokens = _tokenize(text)
    filters: dict[str, str] = {}

    if (
        any(token in text for token in ("ball", "owner", "owned", "with internal", "with external", "with shared"))
        or ("projects" in tokens and any(keyword in tokens for keyword in _BALL_WITH_KEYWORDS))
    ):
        for keyword, value in _BALL_WITH_KEYWORDS.items():
            if keyword in tokens or keyword in text:
                filters["ball_with"] = value
                break

    if "market" in text:
        for market in ("hk", "us", "sg", "jp", "cn"):
            if market in tokens:
                filters["market"] = market.upper()
                break

    if any(word in tokens for word in _STATUS_KEYWORDS) and (
        any(word in tokens for word in _PORTFOLIO_HINTS) or "status" in text or "projects are" in text or "projects with" in text
    ):
        for keyword, value in _STATUS_KEYWORDS.items():
            if keyword in tokens:
                filters["project_status"] = value
                break

    for keyword, value in _MILESTONE_KEYWORDS.items():
        if keyword in tokens:
            filters["current_milestone"] = value
            break

    return filters, None


def _looks_like_broad_portfolio_query(free_text: str) -> bool:
    tokens = _tokenize(free_text)
    has_scope = bool(tokens & {"project", "projects", "portfolio"})
    has_broad_intent = bool(tokens & {"all", "list", "show", "check", "scan", "overview", "which"})
    return has_scope and has_broad_intent


def _parse_query_text(
    free_text: str,
    llm_parse_fn=None,
) -> tuple[dict, str | None, str]:
    filters, search_text = _heuristic_parse_query(free_text)
    if filters or search_text:
        return filters, search_text, "heuristic"
    if _looks_like_broad_portfolio_query(free_text):
        return {}, None, "heuristic"

    if llm_parse_fn is not None:
        try:
            filters, search_text = _normalize_filters(llm_parse_fn(free_text))
            if filters or search_text:
                return filters, search_text, "llm"
        except Exception:
            pass

    return {}, free_text.strip() or None, "fallback_search"


def _infer_project_candidates(conn: sqlite3.Connection, free_text: str) -> list[str]:
    normalized = _normalize_text(free_text)
    rows = conn.execute("SELECT project_id, client_name FROM projects").fetchall()
    candidates: list[str] = []

    for row in rows:
        project_id = row["project_id"]
        client_name = (row["client_name"] or "").strip().lower()
        if project_id.lower() in normalized:
            candidates.append(project_id)
            continue
        if client_name and len(client_name) >= 3 and client_name in normalized:
            candidates.append(project_id)

    return sorted(set(candidates))


def _looks_like_single_project_status(text: str) -> bool:
    normalized = _normalize_text(text)
    if not any(hint in normalized for hint in _STATUS_QUERY_HINTS):
        return False
    return not any(hint in _tokenize(normalized) for hint in _PORTFOLIO_HINTS)


def _detect_requested_fields(text: str) -> list[str]:
    normalized = _normalize_text(text)
    fields: list[str] = []
    if "status" in normalized or "blocked" in normalized or "waiting" in normalized:
        fields.append("project_status")
    if "ball" in normalized or "owner" in normalized or "owned" in normalized:
        fields.append("ball_with")
    if "milestone" in normalized or "stage" in normalized or re.search(r"\bm[1-8]\b", normalized):
        fields.append("current_milestone")
    if "market" in normalized:
        fields.append("market")
    if "client" in normalized:
        fields.append("client_name")
    return fields


def _detect_sort_order(text: str) -> str | None:
    normalized = _normalize_text(text)
    if "latest" in normalized or "recent" in normalized:
        return "updated_at DESC"
    if "oldest" in normalized:
        return "last_activity_at ASC"
    return "project_id ASC"


def _detect_advanced_query_reason(text: str) -> str | None:
    normalized = _normalize_text(text)
    for reason, hints in _ADVANCED_QUERY_HINTS.items():
        if any(hint in normalized for hint in hints):
            return reason
    return None


def _build_scope(filters: dict, search_text: str | None) -> QueryScope:
    project_filter = None
    if filters:
        project_filter = ", ".join(f"{key}={value}" for key, value in sorted(filters.items()))
    elif search_text:
        project_filter = search_text
    return QueryScope(project_filter=project_filter)


def _clarification_for_single_project(project_id: str) -> str:
    return (
        f"This looks like a single-project status request for `{project_id}`. "
        f"Do you want `/status {project_id}` instead, or should I treat it as a portfolio query?"
    )


def _clarification_for_advanced_query(reason: str) -> str:
    if reason == "ranking_or_scoring":
        return (
            "This query needs ranking or scoring logic that is not wired into the aggregate view yet. "
            "Please refine it with explicit filters like status, milestone, ball_with, market, or client name."
        )
    return (
        "This query needs date/aging logic that is not yet available in the aggregate query engine. "
        "Please refine it with explicit filters like status, milestone, ball_with, market, or client name."
    )


def query_projects_from_text(
    conn: sqlite3.Connection,
    free_text: str,
    llm_parse_fn=None,
) -> QueryResult:
    """
    Parse free text into structured query (optionally with LLM).
    Falls back to text search if no LLM.
    """
    filters, search_text, _ = _parse_query_text(free_text, llm_parse_fn=llm_parse_fn)
    return query_projects(
        conn,
        filters=filters or None,
        search_text=search_text,
        exclude_terminal_statuses="project_status" not in filters,
    )


def evaluate_aggregate_query(
    conn: sqlite3.Connection,
    free_text: str,
    *,
    llm_parse_fn=None,
    limit: int = 20,
) -> AggregateQueryEvaluation:
    normalized = _normalize_text(free_text)
    generated_at = datetime.now(timezone.utc).isoformat()
    profile = QueryContextProfile(
        task_type="aggregate_query",
        query_text=free_text.strip(),
        generated_at=generated_at,
        fields_requested=_detect_requested_fields(normalized),
        sort_order=_detect_sort_order(normalized),
        limit=limit,
    )

    if not normalized:
        profile.confidence = 0.0
        profile.escalate_to_operator = True
        profile.escalation_reason = "empty_query"
        return AggregateQueryEvaluation(
            status="needs_clarification",
            profile=profile,
            clarification_question="What portfolio question do you want me to run?",
        )

    advanced_reason = _detect_advanced_query_reason(normalized)
    if advanced_reason:
        profile.confidence = 0.35
        profile.escalate_to_operator = True
        profile.escalation_reason = advanced_reason
        return AggregateQueryEvaluation(
            status="needs_clarification",
            profile=profile,
            clarification_question=_clarification_for_advanced_query(advanced_reason),
        )

    candidates = _infer_project_candidates(conn, free_text)
    if len(candidates) == 1 and _looks_like_single_project_status(normalized):
        profile.scope = QueryScope(project_filter=candidates[0])
        profile.confidence = 0.4
        profile.escalate_to_operator = True
        profile.escalation_reason = "single_project_status_candidate"
        return AggregateQueryEvaluation(
            status="needs_clarification",
            profile=profile,
            clarification_question=_clarification_for_single_project(candidates[0]),
            suggested_filters={"project_id": candidates[0]},
        )

    filters, search_text, parse_source = _parse_query_text(free_text, llm_parse_fn=llm_parse_fn)
    profile.scope = _build_scope(filters, search_text)
    profile.confidence = {
        "heuristic": 0.88 if filters else 0.78,
        "llm": 0.8,
        "fallback_search": 0.62,
    }[parse_source]

    if search_text and len(search_text) < 3 and not filters:
        profile.escalate_to_operator = True
        profile.escalation_reason = "query_too_short"
        return AggregateQueryEvaluation(
            status="needs_clarification",
            profile=profile,
            clarification_question=(
                "That query is too short to run confidently. Please add a client name, status, milestone, market, or owner filter."
            ),
        )

    result = query_projects(
        conn,
        filters=filters or None,
        search_text=search_text,
        limit=limit,
        exclude_terminal_statuses="project_status" not in filters,
    )

    return AggregateQueryEvaluation(
        status="ready",
        profile=profile,
        filters=filters,
        search_text=search_text,
        suggested_filters=filters.copy(),
        result=result,
    )


def format_query_result_text(
    evaluation: AggregateQueryEvaluation,
    *,
    max_rows: int = 10,
) -> str:
    result = evaluation.result or QueryResult(query=evaluation.profile.query_text)
    lines = [
        f"🔎 **Query Result**",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"📋 {evaluation.profile.query_text}",
        f"📊 Found: {result.total} project(s)",
    ]

    if result.total == 0:
        lines.append("")
        lines.append("💡 No matches. Try a different filter.")
        return "\n".join(lines)

    lines.append("")
    for idx, row in enumerate(result.results[:max_rows], start=1):
        status = row.get('project_status') or '-'
        ball = row.get('ball_with') or '-'
        client = row.get('client_name') or '-'
        lines.append(
            f"{idx}. **{row['project_id']}** — {client} · {status} · {ball}"
        )

    if result.total > max_rows:
        lines.append(f"\n💡 Showing top {max_rows}. View all in Dashboard.")

    return "\n".join(lines)


def format_query_clarification_text(evaluation: AggregateQueryEvaluation) -> str:
    lines = [
        "🔎 **Query Clarification**",
        "━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    lines.append(evaluation.clarification_question or "Please clarify the query before I run it.")
    if evaluation.suggested_filters:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(evaluation.suggested_filters.items()))
        lines.append(f"📋 Suggested filters: {rendered}")
    return "\n".join(lines)


def execute_confirmed_query(
    conn: sqlite3.Connection,
    *,
    query_text: str,
    filters: dict | None = None,
    search_text: str | None = None,
    limit: int = 20,
) -> AggregateQueryEvaluation:
    applied_filters = {
        key: value for key, value in (filters or {}).items()
        if key in FILTERABLE_FIELDS and value is not None
    }
    profile = QueryContextProfile(
        task_type="aggregate_query",
        query_text=query_text,
        generated_at=datetime.now(timezone.utc).isoformat(),
        scope=_build_scope(applied_filters, search_text),
        fields_requested=_detect_requested_fields(query_text),
        sort_order=_detect_sort_order(query_text),
        limit=limit,
        confidence=0.95,
        escalate_to_operator=False,
        escalation_reason=None,
    )
    result = query_projects(
        conn,
        filters=applied_filters or None,
        search_text=search_text,
        limit=limit,
        exclude_terminal_statuses="project_status" not in applied_filters,
    )
    return AggregateQueryEvaluation(
        status="ready",
        profile=profile,
        filters=applied_filters,
        search_text=search_text,
        suggested_filters=applied_filters.copy(),
        result=result,
    )
