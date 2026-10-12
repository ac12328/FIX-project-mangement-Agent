"""Portfolio health endpoint — per-project staleness and health color."""

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from src.api.deps import get_conn
from src.api.schemas.portfolio_health import (
    PortfolioHealthResponse,
    PortfolioProjectHealth,
)
from src.skills.aging_detection import EXTERNAL_AGING_RED_DAYS, EXTERNAL_AGING_YELLOW_DAYS

router = APIRouter(tags=["portfolio_health"])


def _compute_health(days_stale: float, has_overdue_followup: bool, has_overdue_internal: bool = False) -> str:
    """Determine health color from staleness and overdue status."""
    if days_stale >= EXTERNAL_AGING_RED_DAYS:
        return "red"
    if days_stale >= EXTERNAL_AGING_YELLOW_DAYS or has_overdue_followup or has_overdue_internal:
        return "yellow"
    return "green"


_HEALTH_ORDER = {"red": 0, "yellow": 1, "green": 2}


@router.get("/portfolio/health", response_model=PortfolioHealthResponse)
def portfolio_health(conn: sqlite3.Connection = Depends(get_conn)):
    """Return health status for all active projects."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        # Active projects
        rows = conn.execute(
            "SELECT project_id, client_name, current_milestone, ball_with, project_tier "
            "FROM projects "
            "WHERE project_status IN ('Active', 'Blocked', 'Waiting')"
            "  AND project_id NOT LIKE '\\_%' ESCAPE '\\'"
        ).fetchall()

        results: list[PortfolioProjectHealth] = []
        for r in rows:
            pid = r["project_id"]

            # Days stale: from last activity_log entry
            last = conn.execute(
                "SELECT MAX(created_at) AS last_at FROM activity_log WHERE project_id = ?",
                (pid,),
            ).fetchone()
            last_at = last["last_at"] if last and last["last_at"] else None
            if last_at:
                try:
                    last_dt = datetime.fromisoformat(last_at).replace(tzinfo=timezone.utc)
                    days_stale = (now - last_dt).total_seconds() / 86400.0
                except (ValueError, TypeError):
                    days_stale = 0.0
            else:
                days_stale = 0.0

            # Pending count: drafts (created/in_review) + open followups + needs_review updates
            pending = 0
            pending += conn.execute(
                "SELECT COUNT(*) AS c FROM drafts "
                "WHERE project_id = ? AND status IN ('created', 'in_review')",
                (pid,),
            ).fetchone()["c"]
            pending += conn.execute(
                "SELECT COUNT(*) AS c FROM internal_followups "
                "WHERE project_id = ? AND status = 'open'",
                (pid,),
            ).fetchone()["c"]
            pending += conn.execute(
                "SELECT COUNT(*) AS c FROM proposed_updates "
                "WHERE project_id = ? AND decision_outcome = 'needs_review'",
                (pid,),
            ).fetchone()["c"]

            # Overdue internal_followups (operator follow-ups, not team actions)
            overdue_followups = conn.execute(
                "SELECT COUNT(*) AS c FROM internal_followups "
                "WHERE project_id = ? AND status = 'open' AND due_date < ?",
                (pid, now_iso),
            ).fetchone()["c"]
            has_overdue = overdue_followups > 0

            # Overdue internal_actions (team-owned tasks)
            now_date = now.strftime("%Y-%m-%d")
            overdue_internal = conn.execute(
                "SELECT COUNT(*) AS c FROM internal_actions "
                "WHERE project_id = ? AND status = 'open' "
                "  AND deadline IS NOT NULL AND deadline < ?",
                (pid, now_date),
            ).fetchone()["c"]

            health = _compute_health(days_stale, has_overdue, overdue_internal > 0)

            results.append(PortfolioProjectHealth(
                project_id=pid,
                client_name=r["client_name"],
                current_milestone=r["current_milestone"],
                ball_with=r["ball_with"],
                project_tier=r["project_tier"],
                days_stale=round(days_stale, 1),
                health=health,
                pending_count=pending,
                overdue_internal_actions=overdue_internal,
                overdue_internal_followups=overdue_followups,
            ))

        # Sort: red first, then yellow, then green
        results.sort(key=lambda x: _HEALTH_ORDER.get(x.health, 9))

        summary = {"green": 0, "yellow": 0, "red": 0}
        for p in results:
            summary[p.health] = summary.get(p.health, 0) + 1

        return PortfolioHealthResponse(projects=results, summary=summary)
    finally:
        conn.row_factory = old_factory
