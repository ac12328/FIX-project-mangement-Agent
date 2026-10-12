"""
Repository for project_corrections — agent self-correction memory (L3).
"""

import sqlite3
import uuid
from datetime import datetime, timezone


def _make_correction_id() -> str:
    return f"cor_{uuid.uuid4().hex[:12]}"


def insert_correction(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    field: str,
    wrong_value: str,
    correct_value: str,
    source: str = "agent_self_correction",
    commit: bool = True,
) -> str:
    """Insert a correction record. Returns correction_id."""
    correction_id = _make_correction_id()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO project_corrections
           (correction_id, project_id, field, wrong_value, correct_value, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (correction_id, project_id, field, wrong_value, correct_value, source, now),
    )
    if commit:
        conn.commit()
    return correction_id


def list_corrections(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    days: int = 90,
    limit: int = 5,
) -> list[dict]:
    """Return recent corrections for a project, newest first."""
    rows = conn.execute(
        """SELECT field, wrong_value, correct_value, created_at
           FROM project_corrections
           WHERE project_id = ?
             AND source IN ('agent_self_correction', 'operator_rejection')
             AND created_at >= datetime('now', ?)
           ORDER BY created_at DESC
           LIMIT ?""",
        (project_id, f"-{days} days", limit),
    ).fetchall()
    return [dict(r) for r in rows]
