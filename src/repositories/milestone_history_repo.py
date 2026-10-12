"""
Milestone history repository.
"""

import sqlite3
from datetime import datetime, timezone


def insert_milestone_change(
    conn: sqlite3.Connection,
    project_id: str,
    from_milestone: str | None,
    to_milestone: str,
    changed_by: str = "system",
    reason: str | None = None,
    evidence_ref: str | None = None,
    proposed_update_id: str | None = None,
) -> None:
    """Record a milestone transition in the audit trail."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO milestone_history
           (project_id, from_milestone, to_milestone, changed_by, changed_at,
            reason, evidence_ref, proposed_update_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, from_milestone, to_milestone, changed_by, now,
         reason, evidence_ref, proposed_update_id),
    )


def get_milestone_history(
    conn: sqlite3.Connection, project_id: str, limit: int = 50
) -> list[dict]:
    """Get milestone history for a project, newest first."""
    rows = conn.execute(
        """SELECT * FROM milestone_history
           WHERE project_id = ?
           ORDER BY changed_at DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
