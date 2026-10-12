"""Repair committed email action receipts from durable outbox outcomes.

The dispatcher can commit a provider receipt and then crash before the approval
record is updated. This reconciliation never calls a provider or guesses from a
still-active claim; an expired claim is quarantined separately as uncertain.
"""

from __future__ import annotations

import json
import sqlite3

from src.email.draft_workspace import utcnow_iso
from src.repositories.action_intent_repo import update_action_status


def reconcile_email_action_receipts(conn: sqlite3.Connection) -> int:
    """Settle executing approvals only when their own outbox has a terminal receipt.

    The action ID is bound into the immutable snapshot at enqueue time. Older
    unlabelled outboxes are deliberately not matched by draft alone: doing so
    could claim another actor's delivery as this action's result.
    """
    actions = conn.execute(
        "SELECT action_id, target_id FROM action_intents "
        "WHERE action_type='email_send' AND status='executing'"
    ).fetchall()
    settled = 0
    for action in actions:
        candidates = conn.execute(
            "SELECT status, snapshot_json, provider_message_id, last_error "
            "FROM draft_outbox WHERE draft_id=? AND status IN "
            "('sent','uncertain','failed','needs_attention','cancelled')",
            (action["target_id"],),
        ).fetchall()
        matches = []
        for outbox in candidates:
            try:
                metadata = json.loads(outbox["snapshot_json"]).get("outbox_meta") or {}
            except (TypeError, ValueError):
                continue
            if metadata.get("action_id") == action["action_id"]:
                matches.append(outbox)
        if len(matches) != 1:
            continue
        outbox = matches[0]
        status = outbox["status"]
        if status == "sent":
            update_action_status(
                conn, action["action_id"], "executed",
                executed_at=utcnow_iso(), result_ref=outbox["provider_message_id"],
            )
        elif status in {"uncertain", "cancelled"}:
            update_action_status(
                conn, action["action_id"], "uncertain",
                error=outbox["last_error"] or "Delivery outcome unconfirmed; manual reconciliation required",
            )
        else:
            update_action_status(
                conn, action["action_id"], "failed",
                error=outbox["last_error"] or "Delivery was not accepted by the provider",
            )
        settled += 1
    return settled
