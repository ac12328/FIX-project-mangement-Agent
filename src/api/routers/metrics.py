"""Effectiveness metrics endpoints."""

import sqlite3

from fastapi import APIRouter, Depends

from src.api.deps import get_read_conn

router = APIRouter(tags=["metrics"])


def _draft_edit_rate(conn: sqlite3.Connection) -> list[dict]:
    """Weekly draft edit rate for sent drafts."""
    rows = conn.execute(
        """
        SELECT
            strftime('%Y-W%W', sent_at) AS week,
            COUNT(*) AS total_sent,
            SUM(CASE WHEN draft_version > 1 THEN 1 ELSE 0 END) AS edited_count,
            ROUND(
                SUM(CASE WHEN draft_version > 1 THEN 1 ELSE 0 END) * 100.0
                / COUNT(*), 1
            ) AS edit_rate
        FROM drafts
        WHERE status = 'sent' AND sent_at IS NOT NULL
        GROUP BY week
        ORDER BY week DESC
        LIMIT 12
        """
    ).fetchall()
    return [
        {
            "week": r["week"],
            "total_sent": r["total_sent"],
            "edited_count": r["edited_count"],
            "edit_rate": r["edit_rate"],
        }
        for r in rows
    ]


def _rule_effectiveness(conn: sqlite3.Connection) -> list[dict]:
    """Top active rules ranked by net score (usage - negatives)."""
    # negative_count column may not exist yet
    try:
        rows = conn.execute(
            """
            SELECT rule_id, rule, usage_count,
                   COALESCE(negative_count, 0) AS negative_count,
                   usage_count - COALESCE(negative_count, 0) AS net_score
            FROM learned_rules
            WHERE status = 'active' AND usage_count > 0
            ORDER BY net_score DESC
            LIMIT 20
            """
        ).fetchall()
    except sqlite3.OperationalError:
        # negative_count column missing — fall back without it
        rows = conn.execute(
            """
            SELECT rule_id, rule, usage_count,
                   0 AS negative_count,
                   usage_count AS net_score
            FROM learned_rules
            WHERE status = 'active' AND usage_count > 0
            ORDER BY net_score DESC
            LIMIT 20
            """
        ).fetchall()

    return [
        {
            "rule_id": r["rule_id"],
            "rule": r["rule"],
            "usage_count": r["usage_count"],
            "negative_count": r["negative_count"],
            "net_score": r["net_score"],
        }
        for r in rows
    ]


def _auto_write_accuracy(conn: sqlite3.Connection) -> dict:
    """Approval vs rejection rate for email pipeline auto-writes."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN decision_outcome = 'accepted' THEN 1 ELSE 0 END) AS approved,
            SUM(CASE WHEN decision_outcome = 'rejected' THEN 1 ELSE 0 END) AS rejected
        FROM proposed_updates
        WHERE task_type = 'truth_write_reasoning'
        """
    ).fetchone()

    total = row["total"] or 0
    approved = row["approved"] or 0
    rejected = row["rejected"] or 0
    approval_rate = round(approved * 100.0 / total, 1) if total > 0 else 0.0

    return {
        "total": total,
        "approved": approved,
        "rejected": rejected,
        "approval_rate": approval_rate,
    }


@router.get("/metrics/effectiveness")
def get_effectiveness_metrics(
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    """Return aggregated effectiveness metrics."""
    return {
        "draft_edit_rate": _draft_edit_rate(conn),
        "rule_effectiveness": _rule_effectiveness(conn),
        "auto_write_accuracy": _auto_write_accuracy(conn),
    }
