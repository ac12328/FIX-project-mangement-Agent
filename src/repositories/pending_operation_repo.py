"""Persistence for owned, expiring pending agent operations."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from src.models.pending_operation import PendingOperation

DEFAULT_PENDING_TTL_MINUTES = 60


def stage_operation(
    conn: sqlite3.Connection,
    *,
    operation_type: str,
    operator_id: str,
    chat_id: str,
    project_id: str | None,
    payload: dict,
    base_version: str | None,
    ttl_minutes: int = DEFAULT_PENDING_TTL_MINUTES,
    request_key: str | None = None,
) -> PendingOperation:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=max(1, ttl_minutes))
    operation_id = f"op_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """UPDATE pending_operations
           SET status='superseded', resolved_at=?, resolution='replaced by newer proposal',
               request_key=NULL
           WHERE operator_id=? AND chat_id=? AND operation_type=?
             AND COALESCE(project_id, '') = COALESCE(?, '') AND status='pending'""",
        (now.isoformat(), operator_id, str(chat_id), operation_type, project_id),
    )
    conn.execute(
        """INSERT INTO pending_operations (
               operation_id, operation_type, operator_id, chat_id, project_id,
               payload_json, base_version, status, created_at, expires_at, request_key
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (
            operation_id, operation_type, operator_id, str(chat_id), project_id,
            json.dumps(payload, ensure_ascii=False), base_version,
            now.isoformat(), expires.isoformat(), request_key,
        ),
    )
    return get_operation(conn, operation_id)  # type: ignore[return-value]


def get_operation_by_request_key(
    conn: sqlite3.Connection, request_key: str | None,
) -> PendingOperation | None:
    if not request_key:
        return None
    row = conn.execute(
        "SELECT * FROM pending_operations WHERE request_key = ?",
        (request_key,),
    ).fetchone()
    return _row(row) if row else None


def clear_request_key(conn: sqlite3.Connection, operation_id: str) -> None:
    conn.execute(
        "UPDATE pending_operations SET request_key=NULL WHERE operation_id=?",
        (operation_id,),
    )


def get_operation(conn: sqlite3.Connection, operation_id: str) -> PendingOperation | None:
    row = conn.execute(
        "SELECT * FROM pending_operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    return _row(row) if row else None


def expire_pending_operations(conn: sqlite3.Connection) -> int:
    """Mark every elapsed pending operation expired and return the row count."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """UPDATE pending_operations
           SET status='expired', resolved_at=?, resolution='expired'
           WHERE status='pending' AND expires_at <= ?""",
        (now, now),
    )
    return int(cur.rowcount or 0)


def expire_operation(conn: sqlite3.Connection, operation_id: str) -> bool:
    """Expire only the selected pending operation inside its command transaction."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """UPDATE pending_operations
           SET status='expired', resolved_at=?, resolution='expired'
           WHERE operation_id=? AND status='pending' AND expires_at <= ?""",
        (now, operation_id, now),
    )
    return cur.rowcount == 1


def list_pending_operations(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    chat_id: str,
    project_id: str | None = None,
    operation_type: str | None = None,
    include_expired: bool = False,
) -> list[PendingOperation]:
    """Find pending rows; include elapsed rows for transactional supersession."""
    clauses = ["operator_id = ?", "chat_id = ?", "status = 'pending'"]
    params: list = [operator_id, str(chat_id)]
    if not include_expired:
        clauses.append("expires_at > ?")
        params.append(datetime.now(timezone.utc).isoformat())
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if operation_type is not None:
        clauses.append("operation_type = ?")
        params.append(operation_type)
    rows = conn.execute(
        f"""SELECT * FROM pending_operations
            WHERE {' AND '.join(clauses)} ORDER BY created_at DESC""",
        params,
    ).fetchall()
    return [_row(row) for row in rows]


def list_project_chat_pending_operations(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    project_id: str,
    operation_type: str = "project_update",
) -> list[PendingOperation]:
    """Pending operations for one project in one chat, regardless of proposer."""
    rows = conn.execute(
        """SELECT * FROM pending_operations
           WHERE chat_id = ? AND project_id = ? AND operation_type = ?
             AND status = 'pending' AND expires_at > ?
           ORDER BY created_at DESC""",
        (str(chat_id), project_id, operation_type, datetime.now(timezone.utc).isoformat()),
    ).fetchall()
    return [_row(row) for row in rows]


def get_recent_resolved_operation(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    chat_id: str,
    project_id: str | None = None,
    operation_type: str | None = None,
    statuses: tuple[str, ...] = ("confirmed",),
) -> PendingOperation | None:
    """Return the newest recently resolved operation for idempotent replies."""
    if not statuses:
        return None
    clauses = ["operator_id = ?", "chat_id = ?"]
    params: list = [operator_id, str(chat_id)]
    placeholders = ", ".join("?" for _ in statuses)
    clauses.append(f"status IN ({placeholders})")
    params.extend(statuses)
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if operation_type is not None:
        clauses.append("operation_type = ?")
        params.append(operation_type)
    row = conn.execute(
        f"""SELECT * FROM pending_operations
            WHERE {' AND '.join(clauses)}
              AND resolved_at >= datetime('now', '-10 minutes')
            ORDER BY resolved_at DESC LIMIT 1""",
        params,
    ).fetchone()
    return _row(row) if row else None


def resolve_operation(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    status: str,
    resolution: str | None = None,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """UPDATE pending_operations
           SET status=?, resolved_at=?, resolution=?
           WHERE operation_id=? AND status='pending'""",
        (status, now, resolution, operation_id),
    )
    return cur.rowcount == 1


def _row(row: sqlite3.Row) -> PendingOperation:
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    data["checkpoint"] = json.loads(data.pop("checkpoint_json", "{}") or "{}")
    data["result"] = json.loads(data.pop("result_json", "{}") or "{}")
    return PendingOperation(**data)
