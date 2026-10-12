"""
AI feedback repository — operator corrections on AI outputs.
"""

import sqlite3
import uuid
from datetime import datetime, timezone


def _gen_id(prefix: str = "") -> str:
    """Generate a short unique ID."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def make_feedback_id() -> str:
    """Generate an AI feedback ID like fb_abc123."""
    return _gen_id("fb_")


def insert_feedback(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    item_id: str,
    project_id: str | None = None,
    feedback_kind: str,
    category: str | None = None,
    operator_note: str | None = None,
    surface: str | None = None,
    trace_id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
    output_revision_id: str | None = None,
) -> str:
    """Insert AI feedback. Returns the feedback_id."""
    feedback_id = make_feedback_id()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO ai_feedback
           (feedback_id, item_type, item_id, project_id,
            feedback_kind, category, operator_note, surface, trace_id,
            run_id, turn_id, output_revision_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            feedback_id, item_type, item_id, project_id, feedback_kind,
            category, operator_note, surface, trace_id, run_id, turn_id,
            output_revision_id, now,
        ),
    )
    return feedback_id


def get_feedback(conn: sqlite3.Connection, feedback_id: str) -> dict | None:
    """Get a single feedback by ID."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM ai_feedback WHERE feedback_id = ?", (feedback_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def list_feedback_for_project(
    conn: sqlite3.Connection, project_id: str, *, limit: int = 50
) -> list[dict]:
    """List all feedback for a project, ordered by created_at DESC."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM ai_feedback
           WHERE project_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_feedback_summary(conn: sqlite3.Connection, *, days: int = 7) -> dict:
    """Return feedback summary for the last N days.

    Returns dict with keys: total, correct, wrong, edited,
    accuracy_pct, top_error_categories.
    """
    conn.row_factory = sqlite3.Row
    cutoff = datetime.now(timezone.utc)
    # Subtract days manually to avoid import of timedelta at module level
    from datetime import timedelta
    cutoff = (cutoff - timedelta(days=days)).isoformat()

    rows = conn.execute(
        """SELECT feedback_kind, COUNT(*) AS cnt
           FROM ai_feedback
           WHERE created_at >= ?
           GROUP BY feedback_kind""",
        (cutoff,),
    ).fetchall()

    counts = {r["feedback_kind"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    correct = counts.get("correct", 0)
    wrong = counts.get("wrong", 0)
    edited = counts.get("edited", 0)
    accuracy_pct = round(correct / total * 100, 1) if total > 0 else 0.0

    # Top error categories (from wrong feedback only)
    cat_rows = conn.execute(
        """SELECT category, COUNT(*) AS cnt
           FROM ai_feedback
           WHERE created_at >= ?
             AND feedback_kind = 'wrong'
             AND category IS NOT NULL
           GROUP BY category
           ORDER BY cnt DESC
           LIMIT 10""",
        (cutoff,),
    ).fetchall()
    top_error_categories = [
        {"category": r["category"], "count": r["cnt"]} for r in cat_rows
    ]

    return {
        "total": total,
        "correct": correct,
        "wrong": wrong,
        "edited": edited,
        "accuracy_pct": accuracy_pct,
        "top_error_categories": top_error_categories,
    }
