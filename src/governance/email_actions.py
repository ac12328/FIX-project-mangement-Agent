"""Build immutable action-intent payloads for outbound email drafts."""

from __future__ import annotations

import sqlite3

from src.email.draft_workspace import (
    build_workspace_snapshot,
    compute_send_affecting_hash,
    initialize_draft_workspace,
)
from src.email.draft_session import get_draft
from src.governance.action_service import canonical_payload_hash, create_action_intent
from src.models.action_intent import ActionIntent


def email_send_payload(conn: sqlite3.Connection, draft_id: str) -> tuple[dict, str]:
    initialize_draft_workspace(conn, draft_id, commit=False)
    snapshot = build_workspace_snapshot(conn, draft_id)
    draft = get_draft(conn, draft_id)
    if draft is None:
        raise ValueError("Draft not found")
    revision = int(snapshot["revision"]["number"])
    snapshot_hash = compute_send_affecting_hash(snapshot)
    payload = {
        "draft_id": draft_id,
        "workspace_revision": revision,
        "send_affecting_hash": snapshot_hash,
        "content": snapshot["content"],
        "envelope": snapshot["envelope"],
        "delivery_context": snapshot["delivery_context"],
        "attachments": [
            {
                "filename": item.get("filename"),
                "size": item.get("size"),
                "sha256": item.get("sha256"),
            }
            for item in snapshot.get("attachments") or []
        ],
        # Legacy callers can still mutate these projections directly. Bind them
        # into the approval payload so such edits invalidate an existing approval
        # even when the workspace revision has not advanced.
        "legacy_projection": {
            "draft_version": int(draft.get("draft_version") or 1),
            "subject": draft.get("subject") or "",
            "current_draft_text": draft.get("current_draft_text") or "",
            "to_email": draft.get("to_email") or "",
            "cc": draft.get("cc") or "",
            "context_packet_ref": draft.get("context_packet_ref"),
        },
    }
    return payload, f"workspace:{revision}"


def create_email_send_intent(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    operator_id: str,
    surface: str,
) -> ActionIntent:
    payload, version = email_send_payload(conn, draft_id)
    return create_action_intent(
        conn,
        operator_id=operator_id,
        action_type="email_send",
        risk_class="high",
        target_type="draft",
        target_id=draft_id,
        target_version=version,
        payload=payload,
        created_by_surface=surface,
        idempotency_key=f"email_send:{draft_id}:{version}:{canonical_payload_hash(payload)}",
    )
