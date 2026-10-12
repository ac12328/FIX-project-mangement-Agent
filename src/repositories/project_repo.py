"""
Project repository — CRUD for the projects table.
project_repo.update_project_fields() is the ONLY function that runs UPDATE projects.
All functions take `conn` as the first argument.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from src.models.project import Project
from src.models.enums import BallWith, Milestone, ProjectStatus


def _row_to_project(row: sqlite3.Row) -> Project:
    """Convert a database row to a Project model."""
    d = dict(row)
    return Project(**d)


def get_project(conn: sqlite3.Connection, project_id: str) -> Project | None:
    """Get a single project by ID."""
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_project(row)


def get_all_projects(conn: sqlite3.Connection) -> list[Project]:
    """Get all projects."""
    rows = conn.execute("SELECT * FROM projects ORDER BY project_id").fetchall()
    return [_row_to_project(r) for r in rows]


def get_active_projects(conn: sqlite3.Connection) -> list[Project]:
    """Get projects with non-terminal statuses."""
    rows = conn.execute(
        "SELECT * FROM projects WHERE project_status IN ('Active','Blocked','Waiting')"
        " AND project_id != '_system' ORDER BY project_id"
    ).fetchall()
    return [_row_to_project(r) for r in rows]


def _iso_timestamp(value: datetime | str | None, default: str) -> str:
    """Normalize optional timestamp fields for INSERT."""
    if value is None:
        return default
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def create_project(
    conn: sqlite3.Connection, project: Project, *, commit: bool = True,
) -> None:
    """Insert a project and work-item envelope, optionally in a caller transaction."""
    now = datetime.now(timezone.utc).isoformat()
    created_at = _iso_timestamp(project.created_at, now)
    updated_at = _iso_timestamp(project.updated_at, now)
    conn.execute(
        """INSERT INTO projects
           (project_id, client_name, client_email, market,
            project_status, ball_with, current_milestone,
            broker_code, connection_type, target_go_live,
            notes, sla_limit, logbook, next_action,
            operator_id, project_tier, created_at, updated_at, last_activity_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project.project_id,
            project.client_name,
            project.client_email,
            project.market,
            project.project_status.value if isinstance(project.project_status, ProjectStatus) else project.project_status,
            project.ball_with.value if isinstance(project.ball_with, BallWith) else project.ball_with,
            project.current_milestone.value if isinstance(project.current_milestone, Milestone) else project.current_milestone,
            project.broker_code,
            project.connection_type,
            project.target_go_live,
            project.notes,
            project.sla_limit,
            project.logbook,
            project.next_action,
            project.operator_id,
            project.project_tier,
            created_at,
            updated_at,
            project.last_activity_at.isoformat() if project.last_activity_at else None,
        ),
    )
    if project.project_id != "_system":
        from src.repositories.work_item_repo import create_work_item
        wi = create_work_item(
            conn, "project",
            project.client_name or project.project_id,
            entity_table="projects",
            entity_id=project.project_id,
        )
        conn.execute(
            "UPDATE projects SET work_item_id = ? WHERE project_id = ?",
            (wi.work_item_id, project.project_id),
        )
    if commit:
        conn.commit()


class StaleProjectWrite(ValueError):
    """Raised when an expected_updated_at compare-and-swap misses."""


def update_project_fields(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict[str, str | None],
    expected_updated_at: str | None = None,
) -> None:
    """
    Update specific fields on a project. This is the ONLY function
    that issues UPDATE projects. Called exclusively by the Write Layer committer.
    """
    if not fields:
        return

    # updated_at is also the CAS token: every serialized write must advance it,
    # even when the OS clock repeats or moves backwards.
    now = datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT updated_at FROM projects WHERE project_id=?", (project_id,),
    ).fetchone()
    if row and row[0]:
        try:
            previous = datetime.fromisoformat(row[0])
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            now = max(now, previous.astimezone(timezone.utc) + timedelta(microseconds=1))
        except (TypeError, ValueError):
            # Legacy malformed timestamps are replaced; the SQL CAS still checks
            # the exact stored token when the caller supplies one.
            pass
    now = now.isoformat()
    # Do not mutate the caller's dict — build a local copy with updated_at.
    # Prevents `updated_at` from leaking into WriteResult.applied_fields and
    # being surfaced to users as a spurious "applied field".
    write_fields = {**fields, "updated_at": now}

    set_clause = ", ".join(f"{k} = ?" for k in write_fields)
    values = list(write_fields.values()) + [project_id]
    where = "project_id = ?"
    if expected_updated_at is not None:
        where += " AND updated_at = ?"
        values.append(expected_updated_at)

    cursor = conn.execute(
        f"UPDATE projects SET {set_clause} WHERE {where}",
        values,
    )
    if expected_updated_at is not None and cursor.rowcount != 1:
        raise StaleProjectWrite(
            "This project changed after you loaded it. Please re-evaluate."
        )
    # Sync work_item status when project_status changes
    if "project_status" in fields:
        from src.portfolio.followup_tracker import resolve_open_followups_for_terminal_project

        resolve_open_followups_for_terminal_project(
            conn, project_id, fields["project_status"],
        )
        row = conn.execute(
            "SELECT work_item_id FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        wi_id = row[0] if row else None
        if wi_id:
            from src.repositories.work_item_repo import update_work_item_status
            from src.models.mapping import project_status_to_work_item_status
            wi_status = project_status_to_work_item_status(fields["project_status"])
            if hasattr(wi_status, "value"):
                wi_status = wi_status.value
            update_work_item_status(conn, wi_id, wi_status)


def get_project_field(
    conn: sqlite3.Connection, project_id: str, field: str
) -> str | None:
    """Get a single field value from a project."""
    from config.defaults import WRITABLE_PROJECT_FIELDS
    _ALLOWED_FIELDS = WRITABLE_PROJECT_FIELDS | {"operator_id", "created_at", "updated_at", "last_activity_at"}
    if field not in _ALLOWED_FIELDS:
        raise ValueError(f"Invalid field name: {field}")
    row = conn.execute(
        f"SELECT {field} FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return None
    return row[0]


def get_active_projects_for_operator(
    conn: sqlite3.Connection,
    operator_id: str | None = None,
) -> list[Project]:
    """Get active projects, optionally filtered by operator_id.

    When operator_id is None, returns all active projects (backward compatible).
    """
    if operator_id is None:
        return get_active_projects(conn)
    rows = conn.execute(
        "SELECT * FROM projects WHERE project_status IN ('Active','Blocked','Waiting') "
        "AND operator_id = ? AND project_id != '_system' ORDER BY project_id",
        (operator_id,),
    ).fetchall()
    return [_row_to_project(r) for r in rows]


def project_exists(conn: sqlite3.Connection, project_id: str) -> bool:
    """Check if a project exists."""
    row = conn.execute(
        "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    return row is not None
