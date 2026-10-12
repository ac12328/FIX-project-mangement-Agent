"""Approval-bound project completion actions.

Project completion is intentionally a single operator approval. The approval
captures the exact project version and the deterministic executor performs the
terminal write after confirming the project is still at M8_GoLive.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.governance.action_service import create_action_intent
from src.models.action_intent import ActionIntent
from src.models.enums import Milestone, ProjectStatus
from src.repositories.action_intent_repo import (
    get_action_intent,
    update_action_status,
)

PROJECT_COMPLETION_ACTION = "project_completion"
_TERMINAL_STATUSES = {status.value for status in ProjectStatus.terminal_statuses()}


def build_project_completion_payload(row: sqlite3.Row) -> dict:
    """Build the human-readable, version-bound completion payload."""
    return {
        "project_id": str(row["project_id"]),
        "client_name": row["client_name"],
        "current_status": str(row["project_status"]),
        "current_milestone": row["current_milestone"],
        "next_action": row["next_action"],
        "effects": {
            "project_status": ProjectStatus.COMPLETED.value,
            "ball_with": "Internal",
            "next_action": "",
        },
    }


def get_project_completion_row(
    conn: sqlite3.Connection,
    project_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT project_id, client_name, project_status, current_milestone,
                  next_action, updated_at
           FROM projects WHERE project_id = ?""",
        (project_id,),
    ).fetchone()


def create_project_completion_intent(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    operator_id: str,
    surface: str = "dashboard",
) -> ActionIntent:
    """Create or reuse the pending one-click completion approval for a project."""
    row = get_project_completion_row(conn, project_id)
    if row is None:
        raise LookupError(f"Project {project_id} not found")

    status = str(row["project_status"])
    if status in _TERMINAL_STATUSES:
        raise ValueError(f"Project {project_id} is already {status}")
    if row["current_milestone"] != Milestone.M8_GO_LIVE.value:
        raise ValueError(
            f"Project completion requires {Milestone.M8_GO_LIVE.value}; "
            f"current milestone is {row['current_milestone'] or 'unset'}"
        )

    existing_row = conn.execute(
        """SELECT action_id FROM action_intents
           WHERE operator_id = ? AND action_type = ?
             AND target_type = 'project' AND target_id = ? AND status = 'pending'
           ORDER BY created_at DESC LIMIT 1""",
        (operator_id, PROJECT_COMPLETION_ACTION, project_id),
    ).fetchone()
    if existing_row is not None:
        existing = get_action_intent(conn, str(existing_row["action_id"]))
        if existing is not None:
            expires_at = datetime.fromisoformat(existing.expires_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if expires_at <= now:
                update_action_status(
                    conn,
                    existing.action_id,
                    "expired",
                    error="Approval expired before confirmation",
                )
            elif existing.target_version == str(row["updated_at"]):
                return existing
            else:
                update_action_status(
                    conn,
                    existing.action_id,
                    "invalidated",
                    error="Project changed after completion approval was created",
                )

    return create_action_intent(
        conn,
        operator_id=operator_id,
        action_type=PROJECT_COMPLETION_ACTION,
        risk_class="high",
        target_type="project",
        target_id=project_id,
        target_version=str(row["updated_at"]),
        payload=build_project_completion_payload(row),
        created_by_surface=surface,
        expires_in_minutes=60,
    )
