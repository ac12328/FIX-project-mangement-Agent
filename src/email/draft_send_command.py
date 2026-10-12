"""Prepare an approved draft's immutable delivery intent without contacting a provider.

The caller owns the transaction when ``commit=False``. In particular, an
approval command can commit the draft transition, snapshot and outbox row
before a separate dispatcher performs external I/O.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from src.email.draft_session import get_draft, update_draft_status, validate_draft_content
from src.email.draft_workspace import (
    build_workspace_snapshot,
    compute_send_affecting_hash,
    initialize_draft_workspace,
    patch_workspace,
    sender_identities,
)
from src.email.outbox import queue_send_now
from src.email.send_gate import SendGateBlockedError
from src.email.sending import SendResult


def queue_approved_draft_send(
    conn: sqlite3.Connection,
    draft_id: str,
    email_config: dict[str, Any],
    *,
    actor_user_id: str,
    confirmed_warnings: list[str] | None = None,
    action_intent: dict[str, Any] | None = None,
    commit: bool = False,
) -> tuple[dict[str, Any] | None, SendResult | None]:
    """Queue a zero-undo send; return either its outbox row or a rejection.

    No external transport is invoked. With ``commit=False``, the caller must
    commit the complete command before any dispatcher may claim the row.
    """
    draft = get_draft(conn, draft_id)
    if not draft or draft["status"] != "approved":
        return None, SendResult(success=False, error=f"Draft {draft_id} not in approved state")
    valid, reason = validate_draft_content(
        draft.get("subject") or "",
        draft.get("current_draft_text") or "",
        for_send=True,
    )
    if not valid:
        update_draft_status(conn, draft_id, "send_failed", commit=commit)
        return None, SendResult(success=False, error=reason)
    if not draft.get("to_email"):
        update_draft_status(conn, draft_id, "send_failed", commit=commit)
        return None, SendResult(success=False, error="Draft has no to_email -- cannot send")

    try:
        initialize_draft_workspace(
            conn, draft_id, actor_user_id=actor_user_id, commit=False,
        )
        snapshot = build_workspace_snapshot(conn, draft_id)
        identities = sender_identities(email_config)
        if not identities:
            update_draft_status(conn, draft_id, "send_failed", commit=commit)
            return None, SendResult(
                success=False,
                error=(
                    "No sender identity configured: set email.from_email or "
                    "email.sender_identities in config.json"
                ),
            )
        identity_ids = {item["identity_id"] for item in identities}
        current_identity_id = snapshot["envelope"].get("sender_identity_id")
        if current_identity_id not in identity_ids:
            selected = next((item for item in identities if item["is_default"]), None) or identities[0]
            patch_workspace(
                conn,
                draft_id,
                {"envelope": {"sender_identity_id": selected["identity_id"]}},
                expected_revision=snapshot["revision"]["number"],
                actor_user_id=actor_user_id,
                commit=False,
                email_config=email_config,
            )
            snapshot = build_workspace_snapshot(conn, draft_id)
        queued = queue_send_now(
            conn,
            draft_id,
            actor_user_id=actor_user_id,
            expected_revision=snapshot["revision"]["number"],
            expected_hash=compute_send_affecting_hash(snapshot),
            confirmed_warnings=confirmed_warnings,
            undo_seconds=0,
            action_intent=action_intent,
            email_config=email_config,
            commit=commit,
        )
        return queued, None
    except SendGateBlockedError as exc:
        return None, SendResult(
            success=False,
            error=f"Draft safety check failed: {json.dumps(exc.result.to_dict(), ensure_ascii=False)}",
        )
    except (ValueError, RuntimeError, sqlite3.Error) as exc:
        return None, SendResult(success=False, error=str(exc))
