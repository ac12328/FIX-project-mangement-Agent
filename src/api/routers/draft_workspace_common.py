"""Shared guards, errors, and events for draft workspace API routers."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import HTTPException

from config import defaults
from src.email.ai_proposals import StaleAIProposalError
from src.email.draft_workspace import (
    WorkspaceConflictError,
    WorkspaceLockedError,
    initialize_draft_workspace,
    public_lease,
    public_preflight,
    public_workspace_snapshot,
)
from src.email.outbox import OutboxStateError
from src.email.send_gate import SendGateBlockedError
from src.email.workspace_collaboration import LeaseConflictError, ReviewStateError
from src.events.bus import Event, event_bus


def require_workspace_enabled() -> None:
    if not defaults.DRAFT_WORKSPACE_ENABLED:
        raise HTTPException(404, "Draft workspace is disabled")


def raise_workspace_api_error(exc: Exception) -> None:
    if isinstance(exc, WorkspaceConflictError):
        raise HTTPException(
            409,
            {
                "code": "revision_conflict",
                "latest": public_workspace_snapshot(exc.snapshot),
            },
        )
    if isinstance(exc, LeaseConflictError):
        raise HTTPException(
            409,
            {"code": "lease_conflict", "lease": public_lease(exc.lease)},
        )
    if isinstance(exc, SendGateBlockedError):
        raise HTTPException(
            422,
            {"code": "preflight_blocked", "preflight": public_preflight(exc.result)},
        )
    if isinstance(exc, PermissionError):
        raise HTTPException(403, str(exc))
    if isinstance(
        exc,
        (
            WorkspaceLockedError,
            ReviewStateError,
            OutboxStateError,
            StaleAIProposalError,
        ),
    ):
        raise HTTPException(422, str(exc))
    if isinstance(exc, ValueError):
        status = 404 if "not found" in str(exc).casefold() else 422
        raise HTTPException(status, str(exc))
    raise exc


def publish_workspace_event(
    draft_id: str,
    event_type: str,
    detail: dict[str, Any] | None = None,
) -> None:
    event_bus.publish(
        Event(
            event_type=event_type,
            detail={"draft_id": draft_id, **(detail or {})},
            surface="dashboard",
        )
    )


def ensure_outbox_draft(
    conn: sqlite3.Connection,
    draft_id: str,
    outbox_id: str,
) -> None:
    row = conn.execute(
        "SELECT 1 FROM draft_outbox WHERE outbox_id=? AND draft_id=?",
        (outbox_id, draft_id),
    ).fetchone()
    if not row:
        raise ValueError("Outbox record not found")


def initialize_legacy_workspace(draft_id: str, conn: sqlite3.Connection, write_scope) -> None:
    """Only legacy first reads require a write; normal views remain read-only."""
    row = conn.execute(
        "SELECT workspace_updated_at FROM drafts WHERE draft_id=?", (draft_id,)
    ).fetchone()
    if not row:
        raise ValueError("Draft not found")
    if row["workspace_updated_at"]:
        return
    with write_scope() as writer:
        row = writer.execute(
            "SELECT workspace_updated_at FROM drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if not row:
            raise ValueError("Draft not found")
        if not row["workspace_updated_at"]:
            initialize_draft_workspace(writer, draft_id, commit=False)
