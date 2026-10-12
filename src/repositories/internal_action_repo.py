import sqlite3
import uuid
from typing import Optional
from src.models.internal_action import InternalAction


def _gen_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def make_action_id() -> str:
    return _gen_id("ia_")


def insert_internal_action(
    conn: sqlite3.Connection,
    project_id: Optional[str],
    owner_team: str,
    title: str,
    deadline: Optional[str] = None,
) -> InternalAction:
    action_id = make_action_id()
    conn.execute(
        """
        INSERT INTO internal_actions
            (action_id, project_id, owner_team, title, deadline)
        VALUES (?, ?, ?, ?, ?)
        """,
        (action_id, project_id, owner_team, title, deadline),
    )
    conn.commit()
    return get_internal_action(conn, action_id)  # type: ignore[return-value]


def get_internal_action(
    conn: sqlite3.Connection, action_id: str
) -> Optional[InternalAction]:
    row = conn.execute(
        "SELECT * FROM internal_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    return InternalAction(**dict(row)) if row else None


def list_internal_actions(
    conn: sqlite3.Connection,
    project_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[InternalAction]:
    q = "SELECT * FROM internal_actions WHERE 1=1"
    params: list = []
    if project_id:
        q += " AND project_id = ?"
        params.append(project_id)
    if status:
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY deadline ASC, created_at ASC"
    rows = conn.execute(q, params).fetchall()
    return [InternalAction(**dict(r)) for r in rows]


def complete_internal_action(
    conn: sqlite3.Connection, action_id: str
) -> Optional[InternalAction]:
    conn.execute(
        """
        UPDATE internal_actions
        SET status = 'done', completed_at = datetime('now')
        WHERE action_id = ?
        """,
        (action_id,),
    )
    conn.commit()
    return get_internal_action(conn, action_id)


def cancel_internal_action(
    conn: sqlite3.Connection, action_id: str
) -> Optional[InternalAction]:
    conn.execute(
        "UPDATE internal_actions SET status = 'cancelled' WHERE action_id = ?",
        (action_id,),
    )
    conn.commit()
    return get_internal_action(conn, action_id)


def count_overdue_internal_actions(
    conn: sqlite3.Connection, project_id: str, now_iso: str
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM internal_actions
        WHERE project_id = ? AND status = 'open'
          AND deadline IS NOT NULL AND deadline < ?
        """,
        (project_id, now_iso),
    ).fetchone()
    return row[0] if row else 0
