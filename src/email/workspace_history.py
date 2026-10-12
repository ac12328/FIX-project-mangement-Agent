"""Checkpoint history operations for the secure draft workspace."""

from __future__ import annotations

import difflib
import json
import sqlite3
import uuid
from typing import Any

from src.email.draft_workspace import (
    WorkspaceConflictError,
    audit_event,
    build_workspace_snapshot,
    compute_send_affecting_hash,
    patch_workspace,
    utcnow_iso,
)


def create_checkpoint(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    reason: str,
    commit: bool = True,
    attachment_data_dir: str | None = None,
) -> dict[str, Any]:
    snapshot = build_workspace_snapshot(
        conn, draft_id, initialize=False, attachment_data_dir=attachment_data_dir,
    )
    version = int(
        conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM draft_checkpoints WHERE draft_id=?",
            (draft_id,),
        ).fetchone()[0]
    )
    snapshot_hash = compute_send_affecting_hash(snapshot)
    checkpoint_id = f"dcp_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """INSERT INTO draft_checkpoints
           (checkpoint_id, draft_id, version, revision, snapshot_json, snapshot_hash,
            reason, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            checkpoint_id,
            draft_id,
            version,
            snapshot["revision"]["number"],
            json.dumps(snapshot, ensure_ascii=False),
            snapshot_hash,
            reason,
            actor_user_id,
            utcnow_iso(),
        ),
    )
    conn.execute(
        "UPDATE drafts SET checkpoint_version=? WHERE draft_id=?", (version, draft_id)
    )
    audit_event(conn, draft_id, "checkpoint_created", actor_user_id, {"version": version, "reason": reason})
    if commit:
        conn.commit()
    return {"checkpoint_id": checkpoint_id, "version": version, "snapshot_hash": snapshot_hash}


def restore_checkpoint(
    conn: sqlite3.Connection,
    draft_id: str,
    version: int,
    *,
    expected_revision: int,
    actor_user_id: str,
    lease_token: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    current = build_workspace_snapshot(conn, draft_id, commit_initialization=commit)
    if current["revision"]["number"] != expected_revision:
        raise WorkspaceConflictError(current)
    row = conn.execute(
        "SELECT snapshot_json FROM draft_checkpoints WHERE draft_id=? AND version=?",
        (draft_id, version),
    ).fetchone()
    if not row:
        raise ValueError("Checkpoint not found")
    saved = json.loads(row["snapshot_json"])
    create_checkpoint(conn, draft_id, actor_user_id=actor_user_id, reason="before_restore", commit=False)
    patch = {
        "content": saved["content"],
        "envelope": saved["envelope"],
        "delivery_context": saved["delivery_context"],
    }
    result = patch_workspace(
        conn,
        draft_id,
        patch,
        expected_revision=expected_revision,
        actor_user_id=actor_user_id,
        lease_token=lease_token,
        commit=False,
    )
    create_checkpoint(conn, draft_id, actor_user_id=actor_user_id, reason=f"restored_v{version}", commit=False)
    if commit:
        conn.commit()
    return result


def compare_checkpoint(conn: sqlite3.Connection, draft_id: str, version: int) -> dict[str, Any]:
    current = build_workspace_snapshot(conn, draft_id)
    row = conn.execute(
        "SELECT snapshot_json FROM draft_checkpoints WHERE draft_id=? AND version=?",
        (draft_id, version),
    ).fetchone()
    if not row:
        raise ValueError("Checkpoint not found")
    saved = json.loads(row["snapshot_json"])
    old_lines = (saved["content"].get("body_text") or "").splitlines()
    new_lines = (current["content"].get("body_text") or "").splitlines()
    return {
        "version": version,
        "subject_before": saved["content"].get("subject"),
        "subject_after": current["content"].get("subject"),
        "body_diff": list(difflib.unified_diff(old_lines, new_lines, fromfile=f"v{version}", tofile="working", lineterm="")),
        "snapshot_before": saved,
        "snapshot_after": current,
    }
