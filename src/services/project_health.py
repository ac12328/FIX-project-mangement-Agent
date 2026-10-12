"""
ProjectHealthService — canonical health scoring, pending item counts, activity recency.

Replaces 4 independent health scoring implementations and 5 pending-item counters.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from src.models.enums import BallWith

logger = logging.getLogger(__name__)


@dataclass
class PendingCounts:
    reviews: int = 0
    drafts: int = 0
    followups: int = 0
    overdue_followups: int = 0


@dataclass
class HealthScore:
    color: str  # 'red' | 'yellow' | 'green'
    score: int  # 0-100
    reasons: list[str] = field(default_factory=list)


def days_since_activity(conn: sqlite3.Connection, project_id: str) -> int | None:
    """Return days since last activity for a project, or None if project not found."""
    row = conn.execute(
        """SELECT CAST(
               (julianday('now') - julianday(COALESCE(last_activity_at, created_at)))
               AS INTEGER) AS days
           FROM projects WHERE project_id = ?""",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return row["days"] if row["days"] is not None else 0


def count_pending_items(
    conn: sqlite3.Connection,
    project_id: str | None = None,
) -> PendingCounts:
    """Return counts of pending reviews, drafts, and followups.

    If project_id is None, counts across all projects.
    """
    if project_id:
        reviews = conn.execute(
            "SELECT COUNT(*) FROM proposed_updates WHERE project_id = ? AND decision_outcome = 'needs_review'",
            (project_id,),
        ).fetchone()[0]

        drafts = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE project_id = ? AND status IN ('created', 'in_review')",
            (project_id,),
        ).fetchone()[0]

        followups = conn.execute(
            "SELECT COUNT(*) FROM internal_followups WHERE project_id = ? AND status = 'open'",
            (project_id,),
        ).fetchone()[0]

        overdue = conn.execute(
            "SELECT COUNT(*) FROM internal_followups WHERE project_id = ? AND status = 'open' AND due_date < date('now')",
            (project_id,),
        ).fetchone()[0]
    else:
        reviews = conn.execute(
            "SELECT COUNT(*) FROM proposed_updates WHERE decision_outcome = 'needs_review'",
        ).fetchone()[0]

        drafts = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE status IN ('created', 'in_review')",
        ).fetchone()[0]

        followups = conn.execute(
            "SELECT COUNT(*) FROM internal_followups WHERE status = 'open'",
        ).fetchone()[0]

        overdue = conn.execute(
            "SELECT COUNT(*) FROM internal_followups WHERE status = 'open' AND due_date < date('now')",
        ).fetchone()[0]

    return PendingCounts(reviews=reviews, drafts=drafts, followups=followups, overdue_followups=overdue)


def compute_project_health(conn: sqlite3.Connection, project_id: str) -> HealthScore:
    """Canonical health computation for a single project.

    Uses AgingDetectionSkill thresholds as the authority.
    Factors: days_since_activity, pending items, SLA, operator flags.
    """
    from src.skills.aging_detection import (
        EXTERNAL_AGING_RED_DAYS,
        EXTERNAL_AGING_YELLOW_DAYS,
    )
    from src.repositories.working_memory_repo import get_memory

    row = conn.execute(
        "SELECT project_status, current_milestone, ball_with, sla_limit FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return HealthScore(color="red", score=0, reasons=["Project not found"])

    days = days_since_activity(conn, project_id) or 0
    pending = count_pending_items(conn, project_id)
    ball_with = row["ball_with"]

    reasons: list[str] = []
    score = 100

    # Aging — use canonical thresholds from AgingDetectionSkill
    if ball_with == BallWith.EXTERNAL.value:
        if days >= EXTERNAL_AGING_RED_DAYS:
            score -= 40
            reasons.append(f"Inactive {days}+ days (red threshold)")
        elif days >= EXTERNAL_AGING_YELLOW_DAYS:
            score -= 20
            reasons.append(f"Inactive {days} days")
    else:
        if days >= EXTERNAL_AGING_RED_DAYS:
            score -= 40
            reasons.append(f"Inactive {days}+ days (red threshold)")

    # Pending items
    if pending.overdue_followups > 0:
        score -= 15
        reasons.append(f"{pending.overdue_followups} overdue followup(s)")
    if pending.reviews > 0:
        score -= 10
        reasons.append(f"{pending.reviews} pending review(s)")

    # Operator flag
    flag_entry = get_memory(conn, project_id, "operator_flag")
    flag = flag_entry["memory_value"] if flag_entry else None
    if flag == "urgent":
        score -= 20
        reasons.append("Flagged urgent by operator")
    elif flag == "watch":
        score -= 5
        reasons.append("Flagged for watch")

    score = max(0, score)
    if score >= 70:
        color = "green"
    elif score >= 40:
        color = "yellow"
    else:
        color = "red"

    return HealthScore(color=color, score=score, reasons=reasons)
