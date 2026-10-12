"""
Proactive Draft Generation — identify stale projects and generate follow-up drafts.

Finds Active projects where ball_with='External' that have gone quiet,
then creates LLM-quality follow-up drafts for operator review via draft_engine.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

from src.db.connection import scoped_read


def identify_draftable_projects(
    conn: sqlite3.Connection,
    *,
    threshold_days: int | None = None,
) -> list[dict[str, Any]]:
    """Find Active/External projects that are stale and have no pending draft.

    Returns list of dicts with project_id, client_name, current_milestone,
    ball_with, last_activity_at — ordered by last_activity_at ASC, limit 10.
    """
    if threshold_days is None:
        from config.settings import CONFIG
        threshold_days = CONFIG.thresholds.stale_external_days
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=threshold_days)).isoformat()

    rows = conn.execute(
        """
        SELECT p.project_id, p.client_name, p.client_email, p.current_milestone,
               p.ball_with, p.last_activity_at
        FROM projects p
        WHERE p.project_status = 'Active'
          AND p.ball_with = 'External'
          AND (p.last_activity_at IS NULL OR p.last_activity_at <= ?)
          AND NOT EXISTS (
              SELECT 1 FROM drafts d
              WHERE d.project_id = p.project_id
                AND d.status IN ('created', 'in_review', 'approved')
          )
        ORDER BY p.last_activity_at ASC
        LIMIT 10
        """,
        (cutoff,),
    ).fetchall()

    return [
        {
            "project_id": row["project_id"],
            "client_name": row["client_name"],
            "client_email": row["client_email"],
            "current_milestone": row["current_milestone"],
            "ball_with": row["ball_with"],
            "last_activity_at": row["last_activity_at"],
        }
        for row in rows
    ]


def generate_proactive_drafts(
    conn: sqlite3.Connection | None,
    *,
    max_drafts: int = 3,
    chat_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Generate LLM-quality follow-up drafts for stale External projects.
    Returns list of dicts with project_id, client_name, draft_id (or None if skipped).
    """
    from src.portfolio.draft_engine import generate_draft_for_signal

    if conn is None:
        with scoped_read() as rconn:
            candidates = identify_draftable_projects(rconn)[:max_drafts]
    else:
        candidates = identify_draftable_projects(conn)[:max_drafts]
    results: list[dict[str, Any]] = []

    for project in candidates:
        project_id = project["project_id"]
        days_stale = _calc_days_waiting(project.get("last_activity_at"))
        context_hints = {
            "days_stale": days_stale,
            "last_outbound_subject": project.get("last_outbound_subject"),
        }
        draft_id = generate_draft_for_signal(
            conn,
            signal_type="stale_external",
            project_id=project_id,
            context_hints=context_hints,
            chat_id=chat_id or 0,
        )
        results.append({
            "project_id": project_id,
            "client_name": project.get("client_name"),
            "draft_id": draft_id,
        })

    return results


def _calc_days_waiting(last_activity_at: str | None, threshold: int | None = None) -> int:
    """Calculate whole days since last_activity_at (or threshold if None)."""
    if threshold is None:
        from config.settings import CONFIG
        threshold = CONFIG.thresholds.stale_external_days
    if not last_activity_at:
        return threshold
    try:
        last_dt = datetime.fromisoformat(last_activity_at)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - last_dt
        return max(0, delta.days)
    except (ValueError, TypeError):
        return 0
