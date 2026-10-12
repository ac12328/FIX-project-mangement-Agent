"""
Core SQL query builders for aggregate project queries.
Extracted from aggregate_query.py to keep file sizes ≤ 500 lines.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from src.models.enums import Milestone as _Milestone, ProjectStatus

FILTERABLE_FIELDS = {
    "project_id",
    "project_status",
    "ball_with",
    "current_milestone",
    "market",
    "client_name",
}


@dataclass
class QueryResult:
    """Result of an aggregate query."""
    query: str
    total: int = 0
    results: list[dict] = field(default_factory=list)
    filters_applied: dict = field(default_factory=dict)


def _normalize_filters(parsed: dict | None) -> tuple[dict, str | None]:
    from src.models.enums import BallWith, Milestone

    _ball_with_keywords = {
        "internal": BallWith.INTERNAL.value,
        "external": BallWith.EXTERNAL.value,
        "shared": BallWith.SHARED.value,
        "unknown": BallWith.UNKNOWN.value,
    }
    _status_keywords = {
        "active": ProjectStatus.ACTIVE.value,
        "blocked": ProjectStatus.BLOCKED.value,
        "waiting": ProjectStatus.WAITING.value,
        "completed": ProjectStatus.COMPLETED.value,
        "archived": ProjectStatus.ARCHIVED.value,
        "cancelled": ProjectStatus.CANCELLED.value,
    }
    _milestone_keywords = {
        "m1": Milestone.M1_NETWORK_SETUP.value,
        "m2": Milestone.M2_UAT_SESSION_CONFIGURATION.value,
        "m3": Milestone.M3_UAT_ENVIRONMENT_ACCESS.value,
        "m4": Milestone.M4_APPLICATION_TESTING.value,
        "m5": Milestone.M5_CONFORMANCE_TEST_CASES.value,
        "m6": Milestone.M6_TEST_REPORT_AND_PRODUCTION_PREPARATION.value,
        "m7": Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY.value,
        "m8": Milestone.M8_GO_LIVE.value,
    }

    if not isinstance(parsed, dict):
        return {}, None

    filters = {k: v for k, v in parsed.items() if k in FILTERABLE_FIELDS and v}
    search_text = parsed.get("search_text")

    if filters.get("ball_with"):
        normalized = str(filters["ball_with"]).strip().lower()
        filters["ball_with"] = _ball_with_keywords.get(normalized, filters["ball_with"])

    if filters.get("project_status"):
        normalized = str(filters["project_status"]).strip().lower()
        filters["project_status"] = _status_keywords.get(normalized, filters["project_status"])

    if filters.get("current_milestone"):
        normalized = str(filters["current_milestone"]).strip().lower().replace(" ", "")
        filters["current_milestone"] = _milestone_keywords.get(normalized, filters["current_milestone"])

    return filters, search_text


def query_projects(
    conn: sqlite3.Connection,
    filters: dict | None = None,
    search_text: str | None = None,
    limit: int = 50,
    exclude_terminal_statuses: bool = False,
    min_days_stale: int | None = None,
    order_by_staleness: bool = False,
    milestone_gte: str | None = None,
    has_overdue_followups: bool | None = None,
) -> QueryResult:
    """
    Query projects with structured filters and optional text search.
    READ ONLY.
    """
    query_str = search_text or str(filters or {})
    result = QueryResult(query=query_str)

    where_clauses = []
    params = []

    if filters:
        for field_name, value in filters.items():
            if field_name in FILTERABLE_FIELDS and value:
                where_clauses.append(f"{field_name} = ?")
                params.append(value)
                result.filters_applied[field_name] = value

    if search_text:
        where_clauses.append("(client_name LIKE ? OR project_id LIKE ? OR notes LIKE ?)")
        like_val = f"%{search_text}%"
        params.extend([like_val, like_val, like_val])

    # Never surface the synthetic system row in portfolio/outstanding lists
    where_clauses.append("project_id != ?")
    params.append("_system")

    if exclude_terminal_statuses:
        where_clauses.append("project_status NOT IN (?, ?, ?)")
        params.extend(
            [
                ProjectStatus.COMPLETED.value,
                ProjectStatus.ARCHIVED.value,
                ProjectStatus.CANCELLED.value,
            ]
        )
        result.filters_applied["exclude_terminal_statuses"] = True

    if min_days_stale is not None:
        where_clauses.append("julianday('now') - julianday(updated_at) >= ?")
        params.append(min_days_stale)
        result.filters_applied["min_days_stale"] = min_days_stale

    if milestone_gte is not None:
        try:
            milestone_values = [m.value for m in _Milestone]
            if milestone_gte in milestone_values:
                min_idx = milestone_values.index(milestone_gte)
                valid_milestones = milestone_values[min_idx:]
                placeholders = ",".join("?" * len(valid_milestones))
                where_clauses.append(f"current_milestone IN ({placeholders})")
                params.extend(valid_milestones)
                result.filters_applied["milestone_gte"] = milestone_gte
        except (ValueError, AttributeError):
            pass  # unknown milestone — no filter applied

    if has_overdue_followups:
        where_clauses.append(
            "project_id IN (SELECT DISTINCT project_id FROM internal_followups WHERE status = 'overdue')"
        )
        result.filters_applied["has_overdue_followups"] = True

    where = " AND ".join(where_clauses) if where_clauses else "1=1"
    order = "updated_at ASC" if order_by_staleness else "project_id ASC"
    count_sql = f"SELECT COUNT(*) FROM projects WHERE {where}"
    count_params = list(params)
    result.total = int(conn.execute(count_sql, count_params).fetchone()[0])

    sql = f"SELECT * FROM projects WHERE {where} ORDER BY {order} LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    result.results = [dict(r) for r in rows]

    return result
