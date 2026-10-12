"""Durable, idempotent operator-update outcome messages."""

from __future__ import annotations

import json
import sqlite3

from src.repositories.assistant_message_clock import next_message_timestamp


def append_resolution_receipt(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    operation_id: str,
    status: str,
    part: dict,
) -> None:
    project_id = part.get("project_id") or "Project"
    fields = part.get("fields") or {}
    if status == "confirmed":
        changes = "; ".join(f"{key} = {value}" for key, value in fields.items())
        text = f"Confirmed: {project_id} updated. {changes}"
    elif status == "cancelled":
        text = f"Cancelled: the proposed update for {project_id} was not applied."
    elif status == "superseded":
        text = f"Not applied: {project_id} changed after this proposal. Review the current project and stage a new update."
    elif status == "expired":
        text = f"Expired: the proposed update for {project_id} was not applied. Stage a new update if still needed."
    else:
        return
    message_id = f"amsg_operator_{operation_id}_{status}"
    cursor = conn.execute(
        """INSERT OR IGNORE INTO assistant_messages(message_id, thread_id, role, content, parts_json, created_at)
           VALUES (?, ?, 'assistant', ?, ?, ?)""",
        (
            message_id,
            thread_id,
            text,
            json.dumps([{"type": "text", "text": text}]),
            next_message_timestamp(conn, thread_id),
        ),
    )
    if cursor.rowcount:
        conn.execute(
            "UPDATE assistant_threads SET updated_at=datetime('now') WHERE thread_id=?",
            (thread_id,),
        )
