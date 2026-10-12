"""Persistence for approval-bound action intents."""

from __future__ import annotations

import json
import sqlite3

from src.models.action_intent import ActionIntent


def insert_action_intent(conn: sqlite3.Connection, intent: ActionIntent) -> None:
    conn.execute(
        """INSERT INTO action_intents (
               action_id, operator_id, action_type, risk_class, target_type,
               target_id, target_version, payload_json, payload_hash, status,
               created_by_surface, approved_by, created_at, expires_at,
               approved_at, executed_at, result_ref, error, idempotency_key
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            intent.action_id,
            intent.operator_id,
            intent.action_type,
            intent.risk_class,
            intent.target_type,
            intent.target_id,
            intent.target_version,
            json.dumps(intent.payload, ensure_ascii=False, sort_keys=True),
            intent.payload_hash,
            intent.status,
            intent.created_by_surface,
            intent.approved_by,
            intent.created_at,
            intent.expires_at,
            intent.approved_at,
            intent.executed_at,
            intent.result_ref,
            intent.error,
            intent.idempotency_key,
        ),
    )


def get_action_intent(
    conn: sqlite3.Connection, action_id: str
) -> ActionIntent | None:
    row = conn.execute(
        "SELECT * FROM action_intents WHERE action_id = ?", (action_id,)
    ).fetchone()
    return _row_to_intent(row) if row else None


def get_action_intent_by_idempotency_key(
    conn: sqlite3.Connection, idempotency_key: str
) -> ActionIntent | None:
    row = conn.execute(
        "SELECT * FROM action_intents WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    return _row_to_intent(row) if row else None


def list_action_intents(
    conn: sqlite3.Connection,
    operator_id: str,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[ActionIntent]:
    if status:
        rows = conn.execute(
            """SELECT * FROM action_intents
               WHERE operator_id = ? AND status = ?
               ORDER BY created_at DESC LIMIT ?""",
            (operator_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM action_intents
               WHERE operator_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (operator_id, limit),
        ).fetchall()
    return [_row_to_intent(row) for row in rows]


def list_pending_team_intents(
    conn: sqlite3.Connection,
    *,
    status: str | None = "pending",
    limit: int = 100,
) -> list[ActionIntent]:
    if status:
        rows = conn.execute(
            """SELECT * FROM action_intents
               WHERE status = ?
               ORDER BY created_at DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM action_intents
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_row_to_intent(row) for row in rows]


def update_action_status(
    conn: sqlite3.Connection,
    action_id: str,
    status: str,
    *,
    approved_by: str | None = None,
    approved_at: str | None = None,
    executed_at: str | None = None,
    result_ref: str | None = None,
    error: str | None = None,
) -> bool:
    cursor = conn.execute(
        """UPDATE action_intents
           SET status = ?,
               approved_by = COALESCE(?, approved_by),
               approved_at = COALESCE(?, approved_at),
               executed_at = COALESCE(?, executed_at),
               result_ref = COALESCE(?, result_ref),
               error = ?
           WHERE action_id = ?""",
        (
            status,
            approved_by,
            approved_at,
            executed_at,
            result_ref,
            error,
            action_id,
        ),
    )
    return cursor.rowcount == 1


def _row_to_intent(row: sqlite3.Row) -> ActionIntent:
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    return ActionIntent(**data)
