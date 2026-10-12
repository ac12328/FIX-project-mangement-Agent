"""Revision-bound AI proposal lifecycle for draft workspaces."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from src.email.draft_workspace import (
    WorkspaceConflictError,
    build_workspace_snapshot,
    compute_send_affecting_hash,
    create_checkpoint,
    patch_workspace,
    utcnow_iso,
)

ALLOWED_ACTIONS = {"rewrite", "tone", "shorten", "translate", "fact_check"}


class StaleAIProposalError(RuntimeError):
    pass


def create_ai_proposal(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    action: str,
    proposal: dict[str, Any],
    base_revision: int | None = None,
    base_hash: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    if action not in ALLOWED_ACTIONS:
        raise ValueError("Unsupported AI proposal action")
    snapshot = build_workspace_snapshot(conn, draft_id, commit_initialization=commit)
    revision = snapshot["revision"]["number"]
    snapshot_hash = compute_send_affecting_hash(snapshot)
    if base_revision is not None and revision != base_revision:
        raise WorkspaceConflictError(snapshot)
    if base_hash is not None and snapshot_hash != base_hash:
        raise StaleAIProposalError("Draft changed before proposal creation")
    proposal_id = f"dap_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """INSERT INTO draft_ai_proposals
           (proposal_id, draft_id, base_revision, base_hash, action,
            proposal_json, status, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (
            proposal_id,
            draft_id,
            revision,
            snapshot_hash,
            action,
            json.dumps(proposal, ensure_ascii=False),
            actor_user_id,
            utcnow_iso(),
        ),
    )
    if commit:
        conn.commit()
    return get_ai_proposal(conn, proposal_id)


def get_ai_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM draft_ai_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError("AI proposal not found")
    data = dict(row)
    data["proposal"] = json.loads(data.pop("proposal_json"))
    return data


def list_ai_proposals(conn: sqlite3.Connection, draft_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM draft_ai_proposals WHERE draft_id=? ORDER BY created_at DESC",
        (draft_id,),
    ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["proposal"] = json.loads(data.pop("proposal_json"))
        result.append(data)
    return result


def apply_ai_proposal(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    actor_user_id: str,
    selected_changes: list[str] | None = None,
    lease_token: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    proposal_row = get_ai_proposal(conn, proposal_id)
    if proposal_row["status"] != "open":
        raise StaleAIProposalError("AI proposal is no longer open")
    snapshot = build_workspace_snapshot(conn, proposal_row["draft_id"], commit_initialization=commit)
    current_hash = compute_send_affecting_hash(snapshot)
    if (
        snapshot["revision"]["number"] != proposal_row["base_revision"]
        or current_hash != proposal_row["base_hash"]
    ):
        conn.execute(
            "UPDATE draft_ai_proposals SET status='stale' WHERE proposal_id=?",
            (proposal_id,),
        )
        if commit:
            conn.commit()
        raise StaleAIProposalError("Draft changed after this proposal was generated")

    proposal = proposal_row["proposal"]
    selected = set(selected_changes or proposal.keys())
    content_patch: dict[str, Any] = {}
    for key in ("subject", "body_html", "body_text"):
        if key in proposal and key in selected:
            content_patch[key] = proposal[key]
    if not content_patch:
        raise ValueError("No proposal changes selected")
    create_checkpoint(
        conn,
        proposal_row["draft_id"],
        actor_user_id=actor_user_id,
        reason=f"before_ai_{proposal_row['action']}",
        commit=False,
    )
    result = patch_workspace(
        conn,
        proposal_row["draft_id"],
        {"content": content_patch},
        expected_revision=proposal_row["base_revision"],
        actor_user_id=actor_user_id,
        lease_token=lease_token,
        commit=False,
    )
    conn.execute(
        "UPDATE draft_ai_proposals SET status='applied', applied_at=? WHERE proposal_id=?",
        (utcnow_iso(), proposal_id),
    )
    create_checkpoint(
        conn,
        proposal_row["draft_id"],
        actor_user_id=actor_user_id,
        reason=f"ai_{proposal_row['action']}_applied",
        commit=False,
    )
    if commit:
        conn.commit()
    return result


def reject_ai_proposal(conn: sqlite3.Connection, proposal_id: str, *, commit: bool = True) -> dict[str, Any]:
    row = get_ai_proposal(conn, proposal_id)
    if row["status"] == "open":
        conn.execute("UPDATE draft_ai_proposals SET status='rejected' WHERE proposal_id=?", (proposal_id,))
        if commit:
            conn.commit()
    return get_ai_proposal(conn, proposal_id)
