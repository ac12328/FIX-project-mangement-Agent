"""
Work item repository — CRUD for the work_items supertype table.
"""

from __future__ import annotations

import sqlite3
import uuid

from src.models.work_item import WorkItem


def _gen_id() -> str:
    return f"wi_{uuid.uuid4().hex[:12]}"


def _row_to_work_item(row: sqlite3.Row) -> WorkItem:
    return WorkItem(**dict(row))


def create_work_item(
    conn: sqlite3.Connection,
    work_item_type: str,
    title: str,
    entity_table: str | None = None,
    entity_id: str | None = None,
    status: str = "open",
    priority: str = "medium",
) -> WorkItem:
    """Insert a new work item. Returns the created WorkItem."""
    work_item_id = _gen_id()
    conn.execute(
        """INSERT INTO work_items
           (work_item_id, work_item_type, title, status, priority,
            entity_table, entity_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (work_item_id, work_item_type, title, status, priority,
         entity_table, entity_id),
    )
    return get_work_item(conn, work_item_id)  # type: ignore[return-value]


def get_work_item(conn: sqlite3.Connection, work_item_id: str) -> WorkItem | None:
    """Get a work item by ID."""
    row = conn.execute(
        "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
    ).fetchone()
    return _row_to_work_item(row) if row else None


def get_work_item_by_entity(
    conn: sqlite3.Connection, entity_table: str, entity_id: str
) -> WorkItem | None:
    """Get a work item by its linked entity table + entity ID."""
    row = conn.execute(
        "SELECT * FROM work_items WHERE entity_table = ? AND entity_id = ?",
        (entity_table, entity_id),
    ).fetchone()
    return _row_to_work_item(row) if row else None


def list_active_work_items(
    conn: sqlite3.Connection,
    type_filter: str | None = None,
    limit: int = 20,
) -> list[WorkItem]:
    """Return open work items. type_filter=None means all types."""
    if type_filter is None:
        rows = conn.execute(
            "SELECT * FROM work_items WHERE status = 'open' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM work_items WHERE status = 'open' AND work_item_type = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (type_filter, limit),
        ).fetchall()
    return [_row_to_work_item(r) for r in rows]


def update_work_item_status(
    conn: sqlite3.Connection, work_item_id: str, status: str
) -> None:
    """Update the status field on a work item."""
    conn.execute(
        "UPDATE work_items SET status = ? WHERE work_item_id = ?",
        (status, work_item_id),
    )
