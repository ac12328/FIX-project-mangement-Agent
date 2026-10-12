"""Agent tools for new-project intake (stage → confirm)."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from src.portfolio.project_intake import (
    IntakeProposal,
    confirm_intake,
    create_intake_from_structured,
)
from src.repositories.project_repo import project_exists
from src.repositories.working_memory_repo import delete_memory, get_memory, upsert_memory

# Store under _system — working_memory.project_id has FK to projects, and the
# target project does not exist until confirm_create_project commits it.
_SYSTEM_PID = "_system"
_PENDING_PREFIX = "intake_proposal:"
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")


def _normalize_project_id(raw: str) -> str:
    return (raw or "").strip().upper()


def _memory_key(project_id: str) -> str:
    return f"{_PENDING_PREFIX}{project_id}"


def ensure_system_project(conn: sqlite3.Connection) -> None:
    """Ensure the _system sentinel project exists for FK-safe staging storage."""
    conn.execute(
        "INSERT OR IGNORE INTO projects "
        "(project_id, project_status, ball_with, client_name, current_milestone) "
        "VALUES (?, 'Archived', 'Internal', 'System', 'M1_NetworkSetup')",
        (_SYSTEM_PID,),
    )


def store_intake_proposal(conn: sqlite3.Connection, proposal: IntakeProposal) -> None:
    """Persist a staged intake proposal under the _system sentinel."""
    ensure_system_project(conn)
    upsert_memory(
        conn,
        _SYSTEM_PID,
        _memory_key(proposal.project_id),
        json.dumps(_proposal_to_memory(proposal), ensure_ascii=False),
    )


def load_intake_proposal(
    conn: sqlite3.Connection, project_id: str,
) -> IntakeProposal | None:
    """Load a staged intake proposal (system key, then legacy per-project key)."""
    mem = get_memory(conn, _SYSTEM_PID, _memory_key(project_id))
    if not mem or not mem.get("memory_value"):
        # Legacy: /newproject used to store under project_id (fails FK when missing)
        mem = get_memory(conn, project_id, "intake_proposal")
    if not mem or not mem.get("memory_value"):
        return None
    try:
        data = json.loads(mem["memory_value"])
    except (json.JSONDecodeError, TypeError):
        return None
    return _proposal_from_memory(project_id, data)


def clear_intake_proposal(conn: sqlite3.Connection, project_id: str) -> bool:
    """Remove staged intake; return True if anything was present."""
    had = False
    for pid, key in (
        (_SYSTEM_PID, _memory_key(project_id)),
        (project_id, "intake_proposal"),
    ):
        mem = get_memory(conn, pid, key)
        if mem:
            delete_memory(conn, pid, key)
            had = True
    return had


def _proposal_summary(proposal: IntakeProposal) -> str:
    lines = [
        f"  project_id: {proposal.project_id}",
        f"  client_name: {proposal.client_name}",
        f"  client_email: {proposal.client_email or '—'}",
        f"  market: {proposal.market or '—'}",
        f"  connection_type: {proposal.connection_type or '—'}",
        f"  broker_code: {proposal.broker_code or '—'}",
        f"  target_go_live: {proposal.target_go_live or '—'}",
        f"  notes: {proposal.notes or '—'}",
        "  initial_milestone: M1_NetworkSetup",
        "  ball_with: Internal",
        "  project_status: Active",
    ]
    return "\n".join(lines)


def _proposal_to_memory(proposal: IntakeProposal) -> dict[str, Any]:
    return {
        "project_id": proposal.project_id,
        "client_name": proposal.client_name,
        "client_email": proposal.client_email,
        "market": proposal.market,
        "connection_type": proposal.connection_type,
        "target_go_live": proposal.target_go_live,
        "broker_code": proposal.broker_code,
        "notes": proposal.notes,
        "source": proposal.source,
    }


def _proposal_from_memory(project_id: str, data: dict[str, Any]) -> IntakeProposal:
    return IntakeProposal(
        project_id=project_id,
        client_name=data.get("client_name") or "Unknown",
        client_email=data.get("client_email"),
        market=data.get("market"),
        connection_type=data.get("connection_type") or "FIX 4.2",
        target_go_live=data.get("target_go_live"),
        broker_code=data.get("broker_code"),
        notes=data.get("notes"),
        source=data.get("source") or "agent",
    )


def chat_create_project(conn: sqlite3.Connection, args: dict) -> dict:
    """Stage a new project intake proposal (does NOT write the project yet)."""
    project_id = _normalize_project_id(args.get("project_id") or "")
    client_name = (args.get("client_name") or "").strip()
    if not project_id:
        return {"text": "Error: project_id is required."}
    if not _PROJECT_ID_RE.match(project_id):
        return {
            "text": (
                f"Error: invalid project_id '{project_id}'. "
                "Use 2–32 alphanumeric characters (optional _ or -)."
            ),
        }
    if not client_name:
        return {"text": "Error: client_name is required."}
    if project_exists(conn, project_id):
        return {
            "text": (
                f"Project {project_id} already exists. "
                "Use get_project_status or update_project instead."
            ),
        }

    vendor_email = (args.get("vendor_email") or "").strip() or None
    notes = (args.get("notes") or "").strip() or None
    if vendor_email:
        vendor_line = f"Vendor email: {vendor_email}"
        notes = f"{notes}\n{vendor_line}".strip() if notes else vendor_line

    proposal = create_intake_from_structured(
        project_id,
        client_name,
        client_email=(args.get("client_email") or "").strip() or None,
        market=(args.get("market") or "").strip() or None,
        connection_type=(args.get("connection_type") or "").strip() or "FIX 4.2",
        target_go_live=(args.get("target_go_live") or "").strip() or None,
        broker_code=(args.get("broker_code") or "").strip() or None,
        notes=notes,
        source="agent",
    )
    store_intake_proposal(conn, proposal)
    summary = _proposal_summary(proposal)
    return {
        "text": (
            f"Proposed NEW project (not created yet):\n{summary}\n\n"
            "Present this summary to the operator. "
            "Only after they explicitly confirm, call confirm_create_project. "
            "To discard, call cancel_create_project. "
            "Alternative slash command: "
            f"/newproject {project_id} {client_name.replace(' ', '')} "
            f"{proposal.client_email or ''}".rstrip()
        ),
        "project_id": project_id,
        "staged": True,
    }


def chat_confirm_create_project(conn: sqlite3.Connection, args: dict) -> dict:
    """Commit a previously staged new-project intake after operator confirmation."""
    project_id = _normalize_project_id(args.get("project_id") or "")
    if not project_id:
        return {"text": "Error: project_id is required."}
    if project_exists(conn, project_id):
        clear_intake_proposal(conn, project_id)
        return {"text": f"Project {project_id} already exists — nothing to create."}

    proposal = load_intake_proposal(conn, project_id)
    if proposal is None:
        return {
            "text": (
                f"No pending project intake for {project_id}. "
                "Call create_project first to stage the proposal."
            ),
        }

    ok, msg = confirm_intake(conn, proposal)
    if ok:
        clear_intake_proposal(conn, project_id)
        return {
            "text": (
                f"{msg}\n"
                "Milestone: M1_NetworkSetup | Ball: Internal | Status: Active\n"
                "You can now add_note, manage_followup, or update_project fields."
            ),
            "project_id": project_id,
            "created": True,
        }
    return {"text": msg, "project_id": project_id, "created": False}


def chat_cancel_create_project(conn: sqlite3.Connection, args: dict) -> dict:
    """Discard a staged new-project intake without creating the project."""
    project_id = _normalize_project_id(args.get("project_id") or "")
    if not project_id:
        return {"text": "Error: project_id is required."}
    if not clear_intake_proposal(conn, project_id):
        return {"text": f"No pending project intake for {project_id}."}
    return {"text": f"Discarded pending new project intake for {project_id}."}


PROJECT_INTAKE_TOOL_HANDLERS: dict[str, Any] = {
    "create_project": chat_create_project,
    "confirm_create_project": chat_confirm_create_project,
    "cancel_create_project": chat_cancel_create_project,
}
