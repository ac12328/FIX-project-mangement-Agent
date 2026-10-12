"""
Activity log repository — append-only, immutable.
"""

import sqlite3
from datetime import datetime, timezone


def insert_activity(
    conn: sqlite3.Connection,
    actor: str,
    action_type: str,
    project_id: str | None = None,
    surface: str | None = None,
    object_ref: str | None = None,
    summary: str | None = None,
    metadata_json: str | None = None,
    *,
    touch_project: bool = True,
) -> int:
    """Insert an activity entry; delivery-only audits may opt out of project aging."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO activity_log
           (project_id, actor, surface, action_type,
            object_ref, summary, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, actor, surface, action_type,
         object_ref, summary, metadata_json, now),
    )
    # Keep last_activity_at in sync so closing/aging checks stay accurate.
    # Direct UPDATE is intentional — routing through update_project_fields()
    # would trigger milestone/field validation that is not needed for a
    # timestamp update, and would introduce a circular import with project_repo.
    if project_id and touch_project:
        conn.execute(
            "UPDATE projects SET last_activity_at = ? WHERE project_id = ?",
            (now, project_id),
        )
    return cursor.lastrowid


def get_recent_activity(
    conn: sqlite3.Connection, project_id: str, limit: int = 50
) -> list[dict]:
    """Get recent activity for a project, newest first."""
    rows = conn.execute(
        """SELECT * FROM activity_log
           WHERE project_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_global_activity(
    conn: sqlite3.Connection, limit: int = 20
) -> list[dict]:
    """Get recent activity across all projects, newest first."""
    rows = conn.execute(
        """SELECT * FROM activity_log
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_filtered_activity(
    conn: sqlite3.Connection,
    *,
    actor: str | None = None,
    action_type: str | None = None,
    project_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Get activity with optional filters, newest first."""
    conditions: list[str] = []
    params: list[str | int] = []
    if actor:
        conditions.append("actor = ?")
        params.append(actor)
    if action_type:
        conditions.append("action_type = ?")
        params.append(action_type)
    if project_id:
        conditions.append("project_id = ?")
        params.append(project_id)
    if since:
        conditions.append("created_at >= ?")
        params.append(since)
    if until:
        conditions.append("created_at <= ?")
        params.append(until)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([limit, offset])
    rows = conn.execute(
        f"SELECT * FROM activity_log{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_distinct_action_types(conn: sqlite3.Connection) -> list[str]:
    """Return all distinct action_type values, sorted alphabetically."""
    rows = conn.execute(
        "SELECT DISTINCT action_type FROM activity_log ORDER BY action_type"
    ).fetchall()
    return [r[0] for r in rows]
