"""Persistence helpers for owned AI Workspace threads, turns, and messages."""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from src.repositories.assistant_message_clock import next_message_timestamp
from src.repositories.operator_update_receipts import append_resolution_receipt


@dataclass(frozen=True)
class Page:
    items: list[dict]
    next_cursor: str | None


class AssistantThreadHasRunningTurnError(RuntimeError):
    """Raised when a thread cannot be deleted while a turn is running."""


def _encode_cursor(created_at: str, item_id: str) -> str:
    raw = json.dumps([created_at, item_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        return str(value[0]), str(value[1])
    except Exception as exc:
        raise ValueError("Invalid cursor") from exc


def _project_ids(conn: sqlite3.Connection, thread_id: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """SELECT project_id FROM assistant_thread_projects
               WHERE thread_id=? ORDER BY position, project_id""",
            (thread_id,),
        ).fetchall()
    ]


def _thread_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    item = dict(row)
    item["web_enabled"] = bool(item["web_enabled"])
    item["project_ids"] = _project_ids(conn, item["thread_id"])
    return item


def create_thread(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    title: str = "New conversation",
    scope_mode: str = "portfolio",
    project_ids: list[str] | None = None,
    web_enabled: bool = False,
    thread_id: str | None = None,
) -> dict:
    active_id = thread_id or f"ath_{uuid4().hex}"
    conn.execute(
        """INSERT INTO assistant_threads (
               thread_id, owner_user_id, title, scope_mode, web_enabled
           ) VALUES (?, ?, ?, ?, ?)""",
        (active_id, owner_user_id, title.strip() or "New conversation", scope_mode, int(web_enabled)),
    )
    replace_thread_projects(conn, active_id, project_ids or [])
    return get_thread(conn, active_id, owner_user_id=owner_user_id)  # type: ignore[return-value]


def get_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    owner_user_id: str,
) -> dict | None:
    row = conn.execute(
        """SELECT t.*,
                  (SELECT content FROM assistant_messages m
                   WHERE m.thread_id=t.thread_id AND m.role IN ('user','assistant')
                   ORDER BY m.created_at DESC, m.message_id DESC LIMIT 1) AS last_message
           FROM assistant_threads t
           WHERE t.thread_id=? AND t.owner_user_id=?""",
        (thread_id, owner_user_id),
    ).fetchone()
    return _thread_dict(conn, row) if row else None


def find_project_thread(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    project_id: str,
) -> dict | None:
    row = conn.execute(
        """SELECT t.*,
                  (SELECT content FROM assistant_messages m
                   WHERE m.thread_id=t.thread_id AND m.role IN ('user','assistant')
                   ORDER BY m.created_at DESC, m.message_id DESC LIMIT 1) AS last_message
           FROM assistant_threads t
           JOIN assistant_thread_projects tp ON tp.thread_id=t.thread_id
           WHERE t.owner_user_id=? AND tp.project_id=? AND t.scope_mode='projects'
                 AND t.archived_at IS NULL
           ORDER BY t.updated_at DESC, t.thread_id DESC LIMIT 1""",
        (owner_user_id, project_id),
    ).fetchone()
    return _thread_dict(conn, row) if row else None


def list_threads(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    search: str | None = None,
    archived: bool = False,
    project_id: str | None = None,
    limit: int = 30,
    cursor: str | None = None,
) -> Page:
    clauses = ["t.owner_user_id=?", "t.archived_at IS NOT NULL" if archived else "t.archived_at IS NULL"]
    params: list[object] = [owner_user_id]
    joins = ""
    if project_id:
        joins = "JOIN assistant_thread_projects tp ON tp.thread_id=t.thread_id"
        clauses.append("tp.project_id=?")
        params.append(project_id)
    if search and search.strip():
        clauses.append("(lower(t.title) LIKE ? OR lower(COALESCE((SELECT content FROM assistant_messages sm WHERE sm.thread_id=t.thread_id ORDER BY sm.created_at DESC, sm.message_id DESC LIMIT 1), '')) LIKE ?)")
        needle = f"%{search.strip().lower()}%"
        params.extend([needle, needle])
    if cursor:
        updated_at, thread_id = _decode_cursor(cursor)
        clauses.append("(t.updated_at < ? OR (t.updated_at = ? AND t.thread_id < ?))")
        params.extend([updated_at, updated_at, thread_id])
    params.append(limit + 1)
    rows = conn.execute(
        f"""SELECT t.*,
                   (SELECT content FROM assistant_messages m
                    WHERE m.thread_id=t.thread_id AND m.role IN ('user','assistant')
                    ORDER BY m.created_at DESC, m.message_id DESC LIMIT 1) AS last_message
            FROM assistant_threads t {joins}
            WHERE {' AND '.join(clauses)}
            ORDER BY t.updated_at DESC, t.thread_id DESC LIMIT ?""",
        params,
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_thread_dict(conn, row) for row in rows]
    next_cursor = _encode_cursor(rows[-1]["updated_at"], rows[-1]["thread_id"]) if has_more else None
    return Page(items=items, next_cursor=next_cursor)


def replace_thread_projects(
    conn: sqlite3.Connection,
    thread_id: str,
    project_ids: list[str],
) -> None:
    conn.execute("DELETE FROM assistant_thread_projects WHERE thread_id=?", (thread_id,))
    for position, project_id in enumerate(dict.fromkeys(project_ids)):
        conn.execute(
            """INSERT INTO assistant_thread_projects (thread_id, project_id, position)
               VALUES (?, ?, ?)""",
            (thread_id, project_id, position),
        )


def update_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    owner_user_id: str,
    title: str | None = None,
    scope_mode: str | None = None,
    project_ids: list[str] | None = None,
    web_enabled: bool | None = None,
    archived: bool | None = None,
) -> dict | None:
    if get_thread(conn, thread_id, owner_user_id=owner_user_id) is None:
        return None
    fields: list[str] = []
    params: list[object] = []
    if title is not None:
        fields.append("title=?")
        params.append(title.strip() or "New conversation")
    if scope_mode is not None:
        fields.append("scope_mode=?")
        params.append(scope_mode)
    if web_enabled is not None:
        fields.append("web_enabled=?")
        params.append(int(web_enabled))
    if archived is not None:
        fields.append("archived_at=" + ("datetime('now')" if archived else "NULL"))
    fields.append("updated_at=datetime('now')")
    params.extend([thread_id, owner_user_id])
    conn.execute(
        f"UPDATE assistant_threads SET {', '.join(fields)} WHERE thread_id=? AND owner_user_id=?",
        params,
    )
    if project_ids is not None:
        replace_thread_projects(conn, thread_id, project_ids)
    return get_thread(conn, thread_id, owner_user_id=owner_user_id)


def delete_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    owner_user_id: str,
) -> bool:
    """Permanently delete one owned thread and its non-FK thread state."""
    owned = conn.execute(
        "SELECT 1 FROM assistant_threads WHERE thread_id=? AND owner_user_id=?",
        (thread_id, owner_user_id),
    ).fetchone()
    if owned is None:
        return False
    running = conn.execute(
        "SELECT 1 FROM assistant_turns WHERE thread_id=? AND execution_status='running' LIMIT 1",
        (thread_id,),
    ).fetchone()
    if running is not None:
        raise AssistantThreadHasRunningTurnError(
            "Cannot delete a conversation while a request is running"
        )

    conn.execute(
        "DELETE FROM thread_artifacts WHERE operator_id=? AND thread_id=?",
        (owner_user_id, thread_id),
    )
    conn.execute(
        "DELETE FROM specialist_sessions WHERE operator_id=? AND thread_id=?",
        (owner_user_id, thread_id),
    )
    deleted = conn.execute(
        "DELETE FROM assistant_threads WHERE thread_id=? AND owner_user_id=?",
        (thread_id, owner_user_id),
    )
    return deleted.rowcount > 0


def list_messages(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    owner_user_id: str,
    limit: int = 80,
    cursor: str | None = None,
) -> Page | None:
    if get_thread(conn, thread_id, owner_user_id=owner_user_id) is None:
        return None
    clauses = ["thread_id=?"]
    params: list[object] = [thread_id]
    if cursor:
        created_at, message_id = _decode_cursor(cursor)
        clauses.append("(created_at < ? OR (created_at = ? AND message_id < ?))")
        params.extend([created_at, created_at, message_id])
    params.append(limit + 1)
    rows = conn.execute(
        f"""SELECT * FROM assistant_messages
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, message_id DESC LIMIT ?""",
        params,
    ).fetchall()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = []
    for row in reversed(page_rows):
        item = dict(row)
        try:
            item["parts"] = json.loads(item.pop("parts_json") or "[]")
        except json.JSONDecodeError:
            item["parts"] = [{"type": "error", "message": "Stored message parts are invalid"}]
        items.append(item)
    next_cursor = _encode_cursor(page_rows[-1]["created_at"], page_rows[-1]["message_id"]) if has_more else None
    return Page(items=items, next_cursor=next_cursor)


def save_message(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    role: str,
    content: str,
    parts: list[dict],
    turn_id: str | None = None,
) -> dict:
    message_id = f"amsg_{uuid4().hex}"
    conn.execute(
        """INSERT INTO assistant_messages (
               message_id, thread_id, turn_id, role, content, parts_json, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (message_id, thread_id, turn_id, role, content, json.dumps(parts, ensure_ascii=False),
         next_message_timestamp(conn, thread_id)),
    )
    conn.execute("UPDATE assistant_threads SET updated_at=datetime('now') WHERE thread_id=?", (thread_id,))
    return {"message_id": message_id, "thread_id": thread_id, "turn_id": turn_id, "role": role, "content": content, "parts": parts}


def create_turn(
    conn: sqlite3.Connection,
    *,
    thread: dict,
    client_turn_id: str,
    user_text: str,
    command: str | None,
) -> tuple[dict, bool]:
    existing = conn.execute(
        "SELECT * FROM assistant_turns WHERE thread_id=? AND client_turn_id=?",
        (thread["thread_id"], client_turn_id),
    ).fetchone()
    if existing:
        return dict(existing), False
    turn_id = f"aturn_{uuid4().hex}"
    conn.execute(
        """INSERT INTO assistant_turns (
               turn_id, thread_id, client_turn_id, command, user_text,
               scope_mode_snapshot, project_ids_snapshot_json,
               web_enabled_snapshot, execution_status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
        (
            turn_id,
            thread["thread_id"],
            client_turn_id,
            command,
            user_text,
            thread["scope_mode"],
            json.dumps(thread.get("project_ids", [])),
            int(thread["web_enabled"]),
        ),
    )
    save_message(
        conn,
        thread_id=thread["thread_id"],
        turn_id=turn_id,
        role="user",
        content=user_text,
        parts=[{"type": "text", "text": user_text}],
    )
    if thread["title"] == "New conversation":
        title = " ".join(user_text.strip().split())[:80] or "New conversation"
        conn.execute(
            "UPDATE assistant_threads SET title=? WHERE thread_id=?",
            (title, thread["thread_id"]),
        )
    row = conn.execute("SELECT * FROM assistant_turns WHERE turn_id=?", (turn_id,)).fetchone()
    return dict(row), True


def get_turn(conn: sqlite3.Connection, turn_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM assistant_turns WHERE turn_id=?", (turn_id,)).fetchone()
    return dict(row) if row else None


def get_turn_messages(conn: sqlite3.Connection, turn_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM assistant_messages WHERE turn_id=? ORDER BY created_at, message_id",
        (turn_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["parts"] = json.loads(item.pop("parts_json") or "[]")
        result.append(item)
    return result


def finish_turn(
    conn: sqlite3.Connection,
    turn_id: str,
    *,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """UPDATE assistant_turns
           SET execution_status=?, error=?, completed_at=datetime('now')
           WHERE turn_id=? AND execution_status='running'""",
        (status, error, turn_id),
    )


def bind_operator_update_part(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    project_id: str,
    operation_id: str,
    status: str,
    payload: dict | None = None,
) -> int:
    """Attach an operation_id to a needs_intake operator_update part."""
    rows = conn.execute(
        "SELECT message_id, parts_json FROM assistant_messages WHERE thread_id=?",
        (thread_id,),
    ).fetchall()
    extra = payload or {}
    updated = 0
    for row in rows:
        try:
            parts = json.loads(row["parts_json"] or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(parts, list):
            continue
        changed = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "operator_update":
                continue
            if part.get("project_id") != project_id:
                continue
            if part.get("operation_id"):
                continue
            part["operation_id"] = operation_id
            part["status"] = status
            for key in (
                "kind", "fields", "missing_criteria", "reason", "transition",
                "base_version", "expires_at", "high_risk",
            ):
                if extra.get(key) is not None:
                    part[key] = extra[key]
            changed = True
        if changed:
            conn.execute(
                "UPDATE assistant_messages SET parts_json=? WHERE message_id=?",
                (json.dumps(parts, ensure_ascii=False), row["message_id"]),
            )
            updated += 1
    return updated


def patch_operator_update_part(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    operation_id: str,
    status: str,
    outcome: str | None = None,
    error: str | None = None,
    record_receipt: bool = True,
) -> int:
    """Rewrite matching parts, optionally omitting a separate outcome message."""
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT message_id, parts_json FROM assistant_messages WHERE thread_id=?",
        (thread_id,),
    ).fetchall()
    updated = 0
    for row in rows:
        try:
            parts = json.loads(row["parts_json"] or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(parts, list):
            continue
        changed = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "operator_update":
                continue
            if part.get("operation_id") != operation_id:
                continue
            if part.get("status") == status and part.get("resolved_at"):
                continue
            part["status"] = status
            part["resolved_at"] = now
            if outcome is not None:
                part["outcome"] = outcome
            if error is not None:
                part["error"] = error
            if record_receipt:
                append_resolution_receipt(
                    conn, thread_id=thread_id, operation_id=operation_id,
                    status=status, part=part,
                )
            changed = True
        if changed:
            parts = [
                part for part in parts
                if not isinstance(part, dict) or part.get("type") != "follow_up_questions"
            ]
            conn.execute(
                "UPDATE assistant_messages SET parts_json=? WHERE message_id=?",
                (json.dumps(parts, ensure_ascii=False), row["message_id"]),
            )
            updated += 1
    return updated


def persist_turn_terminal_message(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    turn_id: str,
    status: str,
    content: str,
    parts: list[dict],
    error: str | None = None,
) -> dict | None:
    """Persist one terminal assistant message and finish the turn atomically.

    The worker owns this operation so a browser disconnect cannot discard a result.
    Repeated calls are idempotent and return the already-persisted assistant message.
    """
    if status not in {"completed", "failed"}:
        raise ValueError(f"Unsupported terminal turn status: {status}")
    row = conn.execute(
        "SELECT execution_status FROM assistant_turns WHERE turn_id=? AND thread_id=?",
        (turn_id, thread_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"Assistant turn not found: {turn_id}")

    existing = conn.execute(
        """SELECT * FROM assistant_messages
           WHERE thread_id=? AND turn_id=? AND role='assistant'
           ORDER BY created_at, message_id LIMIT 1""",
        (thread_id, turn_id),
    ).fetchone()
    if existing is not None:
        item = dict(existing)
        item["parts"] = json.loads(item.pop("parts_json") or "[]")
        if str(row["execution_status"]) == "running":
            finish_turn(conn, turn_id, status=status, error=error)
        return item

    if str(row["execution_status"]) != "running":
        return None

    message = save_message(
        conn,
        thread_id=thread_id,
        turn_id=turn_id,
        role="assistant",
        content=content,
        parts=parts,
    )
    finish_turn(conn, turn_id, status=status, error=error)
    return message
