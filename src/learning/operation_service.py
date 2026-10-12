"""Append-only lifecycle operation state helpers."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta


def create_operation(conn: sqlite3.Connection, *, operation_id: str, operation_type: str,
                     idempotency_key: str, request_hash: str, actor_id: str, actor_role: str,
                     reason: str, effective_at: datetime, slo_seconds: int = 300,
                     selector_kind: str | None = None, selector_value: str | None = None) -> None:
    at = effective_at.isoformat()
    conn.execute("""INSERT INTO memory_lifecycle_operations
      (operation_id,operation_type,idempotency_key,request_hash,actor_id,actor_role,reason,
       selector_kind,selector_value,started_at,effective_at,deadline,status,result_json,created_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending','{}',?)""",
      (operation_id, operation_type, idempotency_key, request_hash, actor_id, actor_role, reason,
       selector_kind, selector_value, at, at, (effective_at + timedelta(seconds=slo_seconds)).isoformat(), at))
    append_operation_event(conn, operation_id=operation_id, status="pending", reason="operation created",
                           effective_at=effective_at, key=f"operation:{operation_id}:pending")


def append_operation_event(conn: sqlite3.Connection, *, operation_id: str, status: str,
                           reason: str, effective_at: datetime, key: str, payload: dict | None = None) -> None:
    event_id = "moe_" + hashlib.sha256(key.encode()).hexdigest()[:24]
    conn.execute("""INSERT OR IGNORE INTO memory_lifecycle_operation_events
      (event_id,operation_id,status,reason,effective_at,payload_json,idempotency_key,created_at)
      VALUES (?,?,?,?,?,?,?,?)""", (event_id, operation_id, status, reason, effective_at.isoformat(),
      json.dumps(payload or {}, sort_keys=True), key, effective_at.isoformat()))


def transition_operation(conn: sqlite3.Connection, *, operation_id: str, status: str,
                         reason: str, at: datetime, payload: dict | None = None) -> None:
    previous = conn.execute("SELECT status FROM memory_lifecycle_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if not previous or previous[0] == status:
        return
    conn.execute("UPDATE memory_lifecycle_operations SET status=?,completed_at=CASE WHEN ?='completed' THEN ? ELSE completed_at END WHERE operation_id=?",
                 (status, status, at.isoformat(), operation_id))
    append_operation_event(conn, operation_id=operation_id, status=status, reason=reason, effective_at=at,
                           key=f"operation:{operation_id}:{status}", payload=payload)
