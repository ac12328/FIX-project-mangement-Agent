"""Evidence-bound manual decisions for indeterminate draft delivery."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from config.settings import CONFIG
from src.email.draft_workspace import audit_event, compute_send_affecting_hash, utcnow_iso
from src.email.recipients import envelope_to_legacy, normalize_envelope, valid_email
from src.email.sending import email_config_dict, record_successful_send_bookkeeping
from src.email.workspace_view import public_outbox
from src.repositories.activity_log_repo import insert_activity


class DraftOutboxReconciliationError(ValueError):
    """An indeterminate delivery cannot be safely resolved from this evidence."""


def _snapshot_review_context(raw: str, expected_hash: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read only delivery identity; never return attachments or body text."""
    try:
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("envelope"), dict):
            raise ValueError("Invalid snapshot envelope")
        if compute_send_affecting_hash(snapshot) != expected_hash:
            raise ValueError("Snapshot does not match queued delivery hash")
        envelope = snapshot["envelope"]
        if any(not isinstance(envelope.get(bucket, []), list) for bucket in ("to", "cc", "bcc")):
            raise ValueError("Invalid snapshot recipients")
        recipients = normalize_envelope(envelope)
        if not recipients["to"]:
            raise ValueError("No original To recipient")
        if any(not valid_email(item["email"]) for bucket in ("to", "cc", "bcc")
               for item in recipients[bucket]):
            raise ValueError("Invalid original recipient address")
        return snapshot, {
            "subject": (snapshot.get("content") or {}).get("subject") or "",
            "recipients": {bucket: [item["email"] for item in recipients[bucket]]
                           for bucket in ("to", "cc", "bcc")},
        }
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise DraftOutboxReconciliationError("Invalid immutable send snapshot") from exc


def list_uncertain_outbox(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """Show the delivery receipt and version without exposing its stored snapshot."""
    rows = conn.execute(
        """SELECT outbox_id,draft_id,status,created_by,created_at,updated_at,
                  attempts,last_error,provider_message_id,provider_result_json,
                  snapshot_json,snapshot_hash
           FROM draft_outbox WHERE status='uncertain'
           ORDER BY updated_at,outbox_id LIMIT ?""",
        (max(1, min(limit, 100)),),
    ).fetchall()
    result = []
    for row in rows:
        item = public_outbox(row)
        item["snapshot_hash"] = row["snapshot_hash"]
        try:
            _snapshot, context = _snapshot_review_context(row["snapshot_json"], row["snapshot_hash"])
            item.update(context)
        except DraftOutboxReconciliationError:
            item.update({"subject": None, "recipients": None,
                         "review_blocked_reason": "Invalid immutable send snapshot"})
        result.append(item)
    return result


def reconcile_uncertain_outbox(
    conn: sqlite3.Connection, *, outbox_id: str, actor: str,
    decision: Literal["confirmed_sent", "confirmed_not_sent"],
    expected_updated_at: str, expected_attempt_count: int,
    provider_evidence_ref: str, recipient_evidence_ref: str,
    full_recipient_set_verified: bool, provider_message_id: str | None = None,
) -> dict[str, Any]:
    """Record a human-attested outcome; never call the transport or replay mail.

    The caller owns the writer transaction. All local state and both audit
    records roll back together if bookkeeping or audit insertion fails.
    """
    if not all(isinstance(item, str) and item.strip() for item in (
        outbox_id, actor, expected_updated_at, provider_evidence_ref, recipient_evidence_ref,
    )):
        raise DraftOutboxReconciliationError("Actor, version and both independent evidence references required")
    if provider_evidence_ref.strip() == recipient_evidence_ref.strip():
        raise DraftOutboxReconciliationError("Separate provider and recipient evidence references required")
    if type(expected_attempt_count) is not int or expected_attempt_count < 1:
        raise DraftOutboxReconciliationError("Valid delivery attempt version required")
    if full_recipient_set_verified is not True:
        raise DraftOutboxReconciliationError("Full To/Cc/Bcc recipient set must be independently verified")
    if decision not in ("confirmed_sent", "confirmed_not_sent"):
        raise DraftOutboxReconciliationError("Unknown reconciliation decision")
    if decision == "confirmed_sent" and not (provider_message_id or "").strip():
        raise DraftOutboxReconciliationError("Confirmed delivery requires a provider message ID")
    if decision == "confirmed_not_sent" and provider_message_id:
        raise DraftOutboxReconciliationError("A confirmed non-delivery cannot carry a sent message ID")
    row = conn.execute(
        """SELECT o.*,d.project_id FROM draft_outbox o JOIN drafts d ON d.draft_id=o.draft_id
           WHERE o.outbox_id=?""", (outbox_id,),
    ).fetchone()
    if row is None:
        raise KeyError(outbox_id)
    if (row["status"] != "uncertain" or row["updated_at"] != expected_updated_at
            or row["attempts"] != expected_attempt_count):
        raise DraftOutboxReconciliationError("Stale or non-reviewable delivery")
    if conn.execute(
        """SELECT 1 FROM draft_outbox WHERE draft_id=? AND outbox_id<>?
           AND status IN ('uncertain','sending','sent','pending','scheduled','undoable') LIMIT 1""",
        (row["draft_id"], outbox_id),
    ).fetchone():
        raise DraftOutboxReconciliationError("Resolve conflicting draft delivery intents first")
    if decision == "confirmed_sent":
        message_id = provider_message_id.strip()
        if conn.execute(
            "SELECT 1 FROM processed_emails WHERE email_id=? LIMIT 1", (message_id,),
        ).fetchone() or conn.execute(
            "SELECT 1 FROM draft_outbox WHERE TRIM(provider_message_id)=? AND outbox_id<>? LIMIT 1",
            (message_id, outbox_id),
        ).fetchone():
            raise DraftOutboxReconciliationError("Provider message ID already recorded")

    snapshot, _review_context = _snapshot_review_context(row["snapshot_json"], row["snapshot_hash"])
    now = utcnow_iso()
    metadata = {
        "outbox_id": outbox_id, "decision": decision,
        "provider_evidence_ref": provider_evidence_ref.strip(),
        "recipient_evidence_ref": recipient_evidence_ref.strip(),
        "full_recipient_set_verified": True, "attempts": row["attempts"],
        "provider_message_id": provider_message_id.strip() if provider_message_id else None,
        "previous_error": row["last_error"],
    }
    conn.execute("SAVEPOINT draft_outbox_reconciliation")
    try:
        new_status = "sent" if decision == "confirmed_sent" else "failed"
        updated = conn.execute(
            """UPDATE draft_outbox SET status=?, provider_message_id=?, sent_at=?,
                      updated_at=?, claimed_by=NULL, claim_expires_at=NULL
               WHERE outbox_id=? AND status='uncertain' AND updated_at=? AND attempts=?""",
            (new_status, metadata["provider_message_id"], now if new_status == "sent" else None,
             now, outbox_id, expected_updated_at, expected_attempt_count),
        )
        if updated.rowcount != 1:
            raise DraftOutboxReconciliationError("Stale delivery review")
        if new_status == "sent":
            envelope = normalize_envelope(snapshot["envelope"])
            to_email, _cc, _bcc = envelope_to_legacy(envelope)
            subject = (snapshot.get("content") or {}).get("subject") or ""
            conn.execute(
                """UPDATE drafts SET delivery_status='sent', status='sent', sent_at=?,
                          sent_version=?, scheduled_send_at=NULL, workspace_updated_at=?
                   WHERE draft_id=?""",
                (now, str((snapshot.get("revision") or {}).get("number") or 1), now, row["draft_id"]),
            )
            identity_id = (snapshot.get("envelope") or {}).get("sender_identity_id")
            config = email_config_dict(CONFIG)
            identity = next((item for item in config.get("sender_identities") or []
                             if item.get("identity_id") == identity_id), None)
            record_successful_send_bookkeeping(
                conn, draft_id=row["draft_id"], message_id=metadata["provider_message_id"],
                to_email=to_email or "", subject=subject, snapshot=snapshot,
                sender_email=(identity or {}).get("email_address") or config.get("from_email"),
                commit=False,
            )
        else:
            conn.execute(
                """UPDATE drafts SET delivery_status='failed', status='send_failed',
                          scheduled_send_at=NULL, workspace_updated_at=? WHERE draft_id=?""",
                (now, row["draft_id"]),
            )
        audit_event(conn, row["draft_id"], "send_reconciled", actor.strip(), metadata)
        insert_activity(
            conn, actor=actor.strip(), action_type="draft_outbox_reconciled",
            project_id=row["project_id"], surface="dashboard", object_ref=outbox_id,
            summary=f"Operator attested outbound delivery: {decision}",
            metadata_json=json.dumps(metadata, ensure_ascii=False), touch_project=False,
        )
        conn.execute("RELEASE SAVEPOINT draft_outbox_reconciliation")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT draft_outbox_reconciliation")
        conn.execute("RELEASE SAVEPOINT draft_outbox_reconciliation")
        raise
    return {"outbox_id": outbox_id, "draft_id": row["draft_id"], "status": new_status,
            "updated_at": now, "attempts": row["attempts"]}
