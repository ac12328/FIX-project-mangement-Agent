"""Admin review of uncertain inbound routes; no automatic route replay."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


class InboundReconciliationError(ValueError):
    """An inbound route cannot be safely attested as complete."""


def list_inbound_reconciliation(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> list[dict[str, Any]]:
    """Show source and classification context without IMAP credentials/body."""
    rows = conn.execute(
        """SELECT w.email_id, w.source_type, w.account_key, w.mailbox,
                  w.uid_validity, w.source_uid, w.attempt_count, w.route_state,
                  w.last_error, w.captured_at, w.updated_at,
                  p.project_id, p.ticket_id, p.sender_email, p.subject,
                  p.received_at, p.body_snippet, w.classification_json
           FROM inbound_email_work w JOIN processed_emails p ON p.email_id=w.email_id
           WHERE w.route_state='needs_reconciliation'
           ORDER BY w.updated_at,w.email_id LIMIT ?""",
        (max(1, min(limit, 100)),),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        classification = json.loads(item.pop("classification_json"))
        item["classification_type"] = classification.get("classification_type")
        result.append(item)
    return result


def confirm_inbound_route_completed(
    conn: sqlite3.Connection, *, email_id: str, actor: str,
    expected_updated_at: str, expected_attempt_count: int,
    domain_evidence_ref: str, delivery_evidence_ref: str,
) -> dict[str, Any]:
    """Attest verified effects and delivery; never execute or replay the route.

    Both evidence references must be independently reviewed. A no-send route
    requires an explicit source-backed no-delivery assertion. The caller owns
    the commit, so a failed audit cannot leave a completed work row behind.
    """
    from src.repositories.activity_log_repo import insert_activity

    if not all((email_id.strip(), actor.strip(), expected_updated_at.strip(),
                domain_evidence_ref.strip(), delivery_evidence_ref.strip())):
        raise InboundReconciliationError("email, actor, version and both evidence references required")
    if (type(expected_attempt_count) is not int or expected_attempt_count < 0):
        raise InboundReconciliationError("valid route attempt version required")
    row = conn.execute(
        """SELECT w.*, p.project_id, p.ticket_id FROM inbound_email_work w
           JOIN processed_emails p ON p.email_id=w.email_id WHERE w.email_id=?""",
        (email_id,),
    ).fetchone()
    if row is None:
        raise KeyError(email_id)
    if (row["route_state"] != "needs_reconciliation"
            or row["updated_at"] != expected_updated_at
            or row["attempt_count"] != expected_attempt_count):
        raise InboundReconciliationError("stale or non-reviewable inbound route")
    try:
        checkpoints = json.loads(row["checkpoint_json"])
        if not isinstance(checkpoints, dict) or "manual_resolution" in checkpoints:
            raise InboundReconciliationError("invalid or already resolved checkpoint")
    except (TypeError, ValueError) as exc:
        raise InboundReconciliationError("invalid checkpoint record") from exc
    checkpoints["manual_resolution"] = {
        "decision": "confirmed_completed", "actor": actor.strip(),
        "domain_evidence_ref": domain_evidence_ref.strip(),
        "delivery_evidence_ref": delivery_evidence_ref.strip(),
    }
    conn.execute("SAVEPOINT confirm_inbound_route")
    try:
        updated = conn.execute(
            """UPDATE inbound_email_work SET route_state='completed',
                      checkpoint_json=?, updated_at=datetime('now')
               WHERE email_id=? AND route_state='needs_reconciliation'
                 AND updated_at=? AND attempt_count=?""",
            (json.dumps(checkpoints, ensure_ascii=False), email_id,
             expected_updated_at, expected_attempt_count),
        )
        if updated.rowcount != 1:
            raise InboundReconciliationError("stale inbound route review")
        insert_activity(
            conn, actor=actor.strip(), action_type="inbound_email_route_reconciled",
            project_id=row["project_id"], surface="dashboard", object_ref=email_id,
            summary="Operator attested completion of inbound route and delivery",
            touch_project=False,
            metadata_json=json.dumps({
                "decision": "confirmed_completed", "source_type": row["source_type"],
                "route_attempts": row["attempt_count"], "ticket_id": row["ticket_id"],
                "previous_error": row["last_error"],
                "domain_evidence_ref": domain_evidence_ref.strip(),
                "delivery_evidence_ref": delivery_evidence_ref.strip(),
            }, ensure_ascii=False),
        )
        conn.execute("RELEASE SAVEPOINT confirm_inbound_route")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT confirm_inbound_route")
        conn.execute("RELEASE SAVEPOINT confirm_inbound_route")
        raise

    return {
        "email_id": email_id, "route_state": "completed",
        "attempt_count": row["attempt_count"],
        "updated_at": conn.execute(
            "SELECT updated_at FROM inbound_email_work WHERE email_id=?", (email_id,),
        ).fetchone()[0],
    }
