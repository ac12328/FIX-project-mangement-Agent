"""Persistence helpers for durable project reconciliation operations."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.project_reconciliation.replay import reviewable_recon_fields

_OPERATION_TYPE = "project_email_reconciliation"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: str | None) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def row_to_operation(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _loads(item.pop("payload_json", None))
    item["checkpoint"] = _loads(item.pop("checkpoint_json", None))
    item["result"] = _loads(item.pop("result_json", None))
    return item


def get_operation(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM pending_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    return row_to_operation(row)


def get_operation_by_request_key(
    conn: sqlite3.Connection, request_key: str | None,
) -> dict[str, Any] | None:
    if not request_key:
        return None
    row = conn.execute(
        "SELECT * FROM pending_operations WHERE request_key=?",
        (request_key,),
    ).fetchone()
    return row_to_operation(row)


def get_operation_by_commit_request_key(
    conn: sqlite3.Connection, request_key: str,
) -> dict[str, Any] | None:
    """Resolve a committed Workspace operation by the turn that actually committed it."""
    rows = conn.execute(
        """SELECT * FROM pending_operations
           WHERE operation_type=? AND phase='committed'
             AND CASE WHEN json_valid(payload_json)
                      THEN json_extract(payload_json, '$.commit_request_key')
                      ELSE NULL END=?
           LIMIT 2""",
        (_OPERATION_TYPE, request_key),
    ).fetchall()
    return row_to_operation(rows[0]) if len(rows) == 1 else None


def bind_commit_request_key(
    conn: sqlite3.Connection, operation_id: str, request_key: str,
) -> None:
    """Bind the committing turn in the same transaction as its durable receipt."""
    operation = get_operation(conn, operation_id)
    if operation is None or operation.get("phase") != "committed":
        raise ValueError("A committed reconciliation receipt is required")
    payload = dict(operation.get("payload") or {})
    payload["commit_request_key"] = request_key
    conn.execute(
        "UPDATE pending_operations SET payload_json=? WHERE operation_id=?",
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), operation_id),
    )


def find_latest_owned_operation(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    chat_id: str,
    trusted_surface: str,
    project_id: str | None = None,
    phases: tuple[str, ...] = ("needs_review", "evaluating", "failed"),
) -> dict[str, Any] | None:
    """Return the latest operation only when that latest operation is resumable.

    Filtering by phase inside SQL can resurrect an older unresolved preview after a
    newer operation has already committed. Read the latest operation for this
    trusted surface across every phase, then decide whether it can resume.
    """
    clauses = ["operation_type=?", "operator_id=?", "chat_id=?", "trusted_surface=?"]
    params: list[Any] = [_OPERATION_TYPE, operator_id, str(chat_id), trusted_surface]
    if project_id:
        clauses.append("resource_key=?")
        params.append(project_id)
    row = conn.execute(
        f"SELECT * FROM pending_operations WHERE {' AND '.join(clauses)} "
        "ORDER BY COALESCE(updated_at, created_at) DESC, rowid DESC LIMIT 1",
        params,
    ).fetchone()
    operation = row_to_operation(row)
    if operation is None:
        return None
    if phases and operation.get("phase") not in phases:
        return None
    return operation


def create_operation(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    chat_id: str,
    project_id: str,
    request_key: str | None,
    requested_commit: bool,
    create_if_missing: bool,
    trusted_surface: str,
    trigger_kind: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = get_operation_by_request_key(conn, request_key)
    if existing is not None:
        return existing
    now = _now()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    operation_id = f"recon_{uuid.uuid4().hex[:20]}"
    conn.execute(
        """INSERT INTO pending_operations (
               operation_id, operation_type, operator_id, chat_id, project_id,
               resource_key, request_key, payload_json, base_version, status, phase,
               checkpoint_json, result_json, attempt_count, updated_at, trigger_kind,
               requested_commit, create_if_missing, trusted_surface, created_at, expires_at
           ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL, 'pending', 'queued',
                     '{}', '{}', 0, ?, ?, ?, ?, ?, ?, ?)""",
        (
            operation_id,
            _OPERATION_TYPE,
            operator_id,
            str(chat_id),
            project_id,
            request_key,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
            now,
            trigger_kind,
            int(requested_commit),
            int(create_if_missing),
            trusted_surface,
            now,
            expires_at,
        ),
    )
    return get_operation(conn, operation_id) or {}


def update_operation(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    phase: str,
    checkpoint: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    base_version: str | None = None,
    project_id: str | None = None,
    proposed_update_id: str | None = None,
    last_error: str | None = None,
    increment_attempt: bool = False,
) -> dict[str, Any]:
    status = "pending"
    resolved_at = None
    resolution = None
    if phase in {"committed", "no_change"}:
        status = "confirmed"
        resolved_at = _now()
        resolution = phase
    elif phase == "superseded":
        status = "superseded"
        resolved_at = _now()
        resolution = "superseded"
    elif phase == "failed":
        status = "failed"
        resolved_at = _now()
        resolution = last_error or "failed"
    conn.execute(
        """UPDATE pending_operations SET
               phase=?, status=?, checkpoint_json=COALESCE(?, checkpoint_json),
               result_json=COALESCE(?, result_json), base_version=COALESCE(?, base_version),
               project_id=COALESCE(?, project_id), proposed_update_id=COALESCE(?, proposed_update_id),
               last_error=?, attempt_count=attempt_count+?, updated_at=?,
               resolved_at=?, resolution=?
           WHERE operation_id=?""",
        (
            phase,
            status,
            json.dumps(checkpoint, ensure_ascii=False, sort_keys=True) if checkpoint is not None else None,
            json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None,
            base_version,
            project_id,
            proposed_update_id,
            last_error,
            int(increment_attempt),
            _now(),
            resolved_at,
            resolution,
            operation_id,
        ),
    )
    return get_operation(conn, operation_id) or {}


def store_review_proposal(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    project_id: str,
    fields: dict[str, Any],
    confidence: float,
    source_ref: str,
    operator_id: str,
    assessment: dict[str, Any] | None = None,
) -> str | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id=?", (project_id,)
    ).fetchone()
    fields = reviewable_recon_fields(fields)
    if not row or not fields:
        return None
    project = dict(row)
    prior = conn.execute(
        "SELECT update_id FROM proposed_updates WHERE operation_id=? LIMIT 1",
        (operation_id,),
    ).fetchone()
    if prior:
        return str(prior[0])
    update_id = f"recon_proposal_{uuid.uuid4().hex[:16]}"
    changes = [
        {
            "field": key,
            "old_value": project.get(key),
            "new_value": value,
            "reason": "Historical email reconciliation",
            "evidence_ref": source_ref,
        }
        for key, value in fields.items()
    ]
    policy_version = assessment.get("policy_version") if assessment else None
    conn.execute(
        """INSERT INTO proposed_updates (
               update_id, project_id, target_project_id, operation_id,
               proposed_changes, task_type, confidence, risk_level,
               requires_operator_review, decision_outcome, operator_id,
               assessment_json, policy_version, source_type, source_ref, created_at
           ) VALUES (?, ?, ?, ?, ?, 'email_reconciliation', ?, 'high',
                     1, 'needs_review', ?, ?, ?, 'email', ?, ?)""",
        (
            update_id,
            project_id,
            project_id,
            operation_id,
            json.dumps(changes, ensure_ascii=False),
            confidence,
            operator_id,
            json.dumps(assessment, ensure_ascii=False, sort_keys=True) if assessment else None,
            policy_version,
            source_ref,
            _now(),
        ),
    )
    return update_id


def link_latest_accepted_write(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    operation_id: str,
    source_ref: str,
    assessment: dict[str, Any] | None = None,
) -> str | None:
    row = conn.execute(
        """SELECT update_id FROM proposed_updates
             WHERE project_id=? AND decision_outcome='accepted'
               AND task_type='direct_write' AND operation_id IS NULL
             ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    if not row:
        return None
    update_id = str(row[0])
    conn.execute(
        """UPDATE proposed_updates SET
               target_project_id=?, operation_id=?, source_type='email',
               source_ref=?, assessment_json=?, policy_version=?
             WHERE update_id=?""",
        (
            project_id,
            operation_id,
            source_ref,
            json.dumps(assessment, ensure_ascii=False, sort_keys=True) if assessment else None,
            assessment.get('policy_version') if assessment else None,
            update_id,
        ),
    )
    return update_id

def refresh_review_proposal(
    conn: sqlite3.Connection,
    *,
    update_id: str | None,
    project_id: str,
    fields: dict[str, Any],
    confidence: float,
    source_ref: str,
    assessment: dict[str, Any] | None = None,
) -> str | None:
    """Rebase an existing pending review onto the current project values."""
    fields = reviewable_recon_fields(fields)
    if not update_id or not fields:
        return None
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id=?", (project_id,)
    ).fetchone()
    if row is None:
        return None
    project = dict(row)
    changes = [
        {
            "field": key,
            "old_value": project.get(key),
            "new_value": value,
            "reason": "Historical email reconciliation (refreshed baseline)",
            "evidence_ref": source_ref,
        }
        for key, value in fields.items()
    ]
    policy_version = assessment.get("policy_version") if assessment else None
    cursor = conn.execute(
        """UPDATE proposed_updates SET
               proposed_changes=?, confidence=?, assessment_json=?, policy_version=?,
               source_type='email', source_ref=?, target_project_id=?,
               rejection_reasons=NULL, decided_at=NULL
             WHERE update_id=? AND decision_outcome='needs_review'""",
        (
            json.dumps(changes, ensure_ascii=False),
            confidence,
            json.dumps(assessment, ensure_ascii=False, sort_keys=True) if assessment else None,
            policy_version,
            source_ref,
            project_id,
            update_id,
        ),
    )
    return update_id if cursor.rowcount == 1 else None


def close_review_proposal_as_superseded(
    conn: sqlite3.Connection,
    *,
    update_id: str | None,
    reason: str,
) -> bool:
    """Remove an obsolete reconciliation proposal from the pending review queue."""
    if not update_id:
        return False
    cursor = conn.execute(
        """UPDATE proposed_updates
              SET decision_outcome='rejected', rejection_reasons=?, decided_at=?
            WHERE update_id=? AND decision_outcome='needs_review'""",
        (
            json.dumps({"tags": ["Superseded"], "reason": reason}, ensure_ascii=False),
            _now(),
            update_id,
        ),
    )
    return cursor.rowcount == 1


def proposed_changes_are_notes_only(raw: str | None) -> bool:
    try:
        changes = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(changes, list) or not changes:
        return False
    fields = {
        str(item.get("field") or "")
        for item in changes
        if isinstance(item, dict)
    }
    fields.discard("")
    return fields <= {"notes"}


def close_notes_only_reconciliation_reviews(
    conn: sqlite3.Connection,
    *,
    project_id: str | None = None,
) -> int:
    """Drop recon reviews that only append a notes stamp."""
    clauses = [
        "decision_outcome='needs_review'",
        "task_type='email_reconciliation'",
    ]
    params: list[Any] = []
    if project_id:
        clauses.append("project_id=?")
        params.append(project_id)
    rows = conn.execute(
        f"""SELECT update_id, proposed_changes FROM proposed_updates
             WHERE {' AND '.join(clauses)}""",
        params,
    ).fetchall()
    closed = 0
    for row in rows:
        if not proposed_changes_are_notes_only(row["proposed_changes"]):
            continue
        if close_review_proposal_as_superseded(
            conn,
            update_id=str(row["update_id"]),
            reason="Notes-only email reconciliation is bookkeeping, not a review.",
        ):
            closed += 1
    return closed


def supersede_other_owned_operations(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    chat_id: str,
    project_id: str,
    winning_operation_id: str,
) -> list[str]:
    """Resolve every unfinished sibling after one reconciliation becomes authoritative."""
    winner = conn.execute(
        "SELECT 1 FROM pending_operations WHERE operation_id=?",
        (winning_operation_id,),
    ).fetchone()
    if winner is None:
        return []
    rows = conn.execute(
        """SELECT operation_id, proposed_update_id
             FROM pending_operations
            WHERE operation_type=? AND operator_id=? AND chat_id=?
              AND resource_key=? AND operation_id<>?
              AND phase IN ('queued', 'evaluating', 'needs_review', 'failed')
            ORDER BY rowid""",
        (
            _OPERATION_TYPE,
            operator_id,
            str(chat_id),
            project_id,
            winning_operation_id,
        ),
    ).fetchall()
    if not rows:
        return []

    now = _now()
    superseded_ids: list[str] = []
    for row in rows:
        operation_id = str(row[0])
        result = {
            "ok": True,
            "status": "superseded",
            "outcome": "superseded",
            "committed": False,
            "project_id": project_id,
            "operation_id": operation_id,
            "text": f"Superseded by newer reconciliation operation {winning_operation_id}.",
        }
        conn.execute(
            """UPDATE pending_operations SET
                   phase='superseded', status='superseded', result_json=?,
                   last_error=NULL, updated_at=?, resolved_at=?, resolution=?
                 WHERE operation_id=?""",
            (
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                now,
                now,
                f"superseded_by:{winning_operation_id}",
                operation_id,
            ),
        )
        close_review_proposal_as_superseded(
            conn,
            update_id=str(row[1]) if row[1] else None,
            reason=f"Superseded by newer reconciliation operation {winning_operation_id}.",
        )
        superseded_ids.append(operation_id)
    return superseded_ids

