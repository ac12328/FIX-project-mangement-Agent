"""Shared draft read, edit, and discard helpers.

Approval/delivery belongs to the surface-authorized committed command, not a
second service that sends and publishes before checking the durable receipt.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from src.events.bus import Event, event_bus
from src.repositories.activity_log_repo import insert_activity

logger = logging.getLogger(__name__)


def draft_discard(
    conn: sqlite3.Connection,
    draft_id: str,
    actor: str,
    surface: str,
) -> None:
    """Discard a draft. Single code path for all surfaces."""
    draft = conn.execute(
        "SELECT draft_id, project_id, status FROM drafts WHERE draft_id = ?",
        (draft_id,),
    ).fetchone()
    if not draft:
        logger.warning("draft_discard: draft %s not found", draft_id)
        return

    conn.execute(
        "UPDATE drafts SET status='discarded' WHERE draft_id=?",
        (draft_id,),
    )
    insert_activity(
        conn,
        actor=actor,
        action_type="draft_discarded",
        project_id=draft["project_id"],
        surface=surface,
        object_ref=draft_id,
        summary=f"Draft {draft_id} discarded",
    )
    conn.commit()

    event_bus.publish(Event(
        event_type="draft_discarded",
        project_id=draft["project_id"],
        detail={"draft_id": draft_id, "surface": surface},
        surface=surface,
    ))


def draft_edit(
    conn: sqlite3.Connection,
    draft_id: str,
    new_text: str,
    actor: str,
    surface: str,
) -> dict:
    """Edit a draft's text. Single code path for all surfaces."""
    draft = conn.execute(
        "SELECT draft_id, project_id, draft_version, current_draft_text, draft_history "
        "FROM drafts WHERE draft_id = ?",
        (draft_id,),
    ).fetchone()
    if not draft:
        return {"error": f"Draft {draft_id} not found"}

    history = json.loads(draft["draft_history"] or "[]")
    history.append({"version": draft["draft_version"], "text": draft["current_draft_text"]})

    new_version = draft["draft_version"] + 1
    conn.execute(
        """UPDATE drafts
           SET current_draft_text=?, draft_version=?, draft_history=?
           WHERE draft_id=?""",
        (new_text, new_version, json.dumps(history), draft_id),
    )
    insert_activity(
        conn,
        actor=actor,
        action_type="draft_edited",
        project_id=draft["project_id"],
        surface=surface,
        object_ref=draft_id,
        summary=f"Draft {draft_id} edited to version {new_version}",
    )
    conn.commit()

    event_bus.publish(Event(
        event_type="draft_edited",
        project_id=draft["project_id"],
        detail={"draft_id": draft_id, "version": new_version, "surface": surface},
        surface=surface,
    ))

    return {"draft_id": draft_id, "draft_version": new_version, "current_draft_text": new_text}


def list_drafts(
    conn: sqlite3.Connection,
    project_id: str | None = None,
    *,
    status: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """List drafts, optionally filtered by project and/or status."""
    clauses = []
    params: list = []
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT draft_id, project_id, task_type, status, draft_version, "
        f"to_email, created_at FROM drafts {where} "
        f"ORDER BY created_at DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def get_draft(conn: sqlite3.Connection, draft_id: str) -> dict | None:
    """Get a single draft by ID, including body."""
    row = conn.execute(
        "SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    return dict(row) if row else None
