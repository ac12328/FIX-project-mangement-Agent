"""Repository for operator_memory table."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.models.operator_memory import MemoryType, OperatorMemoryEntry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_memory(conn: sqlite3.Connection, entry: OperatorMemoryEntry) -> None:
    """Insert or update an operator memory row."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "operator_memory")
    now = _now()
    conn.execute(
        """
        INSERT INTO operator_memory (
            operator_id, scope, scope_id, memory_type, memory_key,
            memory_value, source, active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(operator_id, scope, scope_id, memory_type, memory_key)
        DO UPDATE SET
            memory_value = excluded.memory_value,
            source = excluded.source,
            active = excluded.active,
            updated_at = excluded.updated_at
        """,
        (
            entry.operator_id,
            entry.scope,
            entry.scope_id,
            entry.memory_type.value,
            entry.memory_key,
            json.dumps(entry.memory_value, ensure_ascii=False),
            entry.source,
            1 if entry.active else 0,
            now,
            now,
        ),
    )


def get_memory(
    conn: sqlite3.Connection,
    operator_id: str,
    scope: str,
    scope_id: str,
    memory_type: str,
    memory_key: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM operator_memory
        WHERE operator_id = ? AND scope = ? AND scope_id = ?
          AND memory_type = ? AND memory_key = ? AND active = 1
        """,
        (operator_id, scope, scope_id, memory_type, memory_key),
    ).fetchone()
    from src.memory.projection_authority import operator_projection_is_current
    return _row_to_dict(row) if row and operator_projection_is_current(conn, row) else None


def list_memories(
    conn: sqlite3.Connection,
    operator_id: str,
    *,
    scope: str | None = None,
    scope_id: str | None = None,
    memory_type: str | None = None,
) -> list[dict]:
    clauses = ["operator_id = ?", "active = 1"]
    params: list[Any] = [operator_id]
    if scope is not None:
        clauses.append("scope = ?")
        params.append(scope)
    if scope_id is not None:
        clauses.append("scope_id = ?")
        params.append(scope_id)
    if memory_type is not None:
        clauses.append("memory_type = ?")
        params.append(memory_type)
    sql = f"SELECT * FROM operator_memory WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC"
    from src.memory.projection_authority import operator_projection_is_current
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()
            if operator_projection_is_current(conn, r)]


def deactivate_memory(
    conn: sqlite3.Connection,
    operator_id: str,
    scope: str,
    scope_id: str,
    memory_type: str,
    memory_key: str,
) -> bool:
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "operator_memory")
    cur = conn.execute(
        """
        UPDATE operator_memory SET active = 0, updated_at = ?
        WHERE operator_id = ? AND scope = ? AND scope_id = ?
          AND memory_type = ? AND memory_key = ?
        """,
        (_now(), operator_id, scope, scope_id, memory_type, memory_key),
    )
    return cur.rowcount > 0


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    raw = d.get("memory_value") or "{}"
    try:
        d["memory_value"] = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        d["memory_value"] = {"text": str(raw)}
    d["active"] = bool(d.get("active", 1))
    return d