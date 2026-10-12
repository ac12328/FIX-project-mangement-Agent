"""Per-chat active project binding for multi-turn Telegram conversations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

ACTIVE_PROJECT_TTL_HOURS = 4


def _legacy_memory_key(chat_id: int) -> str:
    return f"active_project_{chat_id}"


def _memory_key(chat_id: int, operator_id: str | None = None) -> str:
    if operator_id:
        return f"active_project_{chat_id}_{operator_id}"
    return _legacy_memory_key(chat_id)


def _ensure_system_project(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO projects (project_id, project_status, ball_with, current_milestone)
           VALUES ('_system', 'Archived', 'Internal', 'M1_NetworkSetup')""",
    )


def resolve_active_project_id(
    conn: sqlite3.Connection | None,
    chat_id: int,
    operator_id: str | None = None,
) -> str | None:
    """Read active project for a chat, using scoped_read when conn is None."""
    if conn is not None:
        return get_active_project_id(conn, chat_id, operator_id=operator_id)
    from src.db.connection import scoped_read

    with scoped_read() as rconn:
        return get_active_project_id(rconn, chat_id, operator_id=operator_id)


def _decode_active_project(entry: dict | None) -> str | None:
    if not entry or not entry.get("memory_value"):
        return None
    try:
        data = json.loads(entry["memory_value"])
    except (json.JSONDecodeError, TypeError):
        return None
    touched_at = data.get("touched_at") or ""
    if touched_at:
        try:
            ts = datetime.fromisoformat(touched_at.rstrip("Z")).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - ts > timedelta(hours=ACTIVE_PROJECT_TTL_HOURS):
                return None
        except ValueError:
            pass
    pid = data.get("project_id")
    return pid if isinstance(pid, str) and pid else None


def get_active_project_id(
    conn: sqlite3.Connection,
    chat_id: int,
    operator_id: str | None = None,
) -> str | None:
    from src.repositories.working_memory_repo import get_memory

    if operator_id:
        scoped = _decode_active_project(
            get_memory(conn, "_system", _memory_key(chat_id, operator_id))
        )
        if scoped:
            return scoped
        return _decode_active_project(
            get_memory(conn, "_system", _legacy_memory_key(chat_id))
        )
    return _decode_active_project(
        get_memory(conn, "_system", _legacy_memory_key(chat_id))
    )


def set_active_project_id(
    conn: sqlite3.Connection,
    chat_id: int,
    project_id: str,
    operator_id: str | None = None,
) -> None:
    from src.repositories.working_memory_repo import upsert_memory

    _ensure_system_project(conn)
    payload = json.dumps({
        "project_id": project_id,
        "touched_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False)
    upsert_memory(conn, "_system", _memory_key(chat_id, operator_id), payload)


def clear_active_project_id(
    conn: sqlite3.Connection,
    chat_id: int,
    operator_id: str | None = None,
) -> None:
    from src.repositories.working_memory_repo import delete_memory

    delete_memory(conn, "_system", _memory_key(chat_id, operator_id))