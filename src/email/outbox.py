"""Durable immutable outbox with Undo Send, scheduling, and idempotent dispatch."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.email.outbox_support import (
    CLAIM_SECONDS,
    _as_utc,
    _claim_next,
    _claim_specific,
    _copy_immutable_snapshot,
    _deserialize_outbox,
    _mark_needs_attention,
    _quarantine_expired_claims,
)

from config.settings import CONFIG
from src.email.draft_workspace import (
    audit_event,
    build_workspace_snapshot,
    compute_send_affecting_hash,
    create_checkpoint,
    utcnow_iso,
)
from src.email.recipients import envelope_to_legacy, normalize_envelope
from src.email.send_gate import SendGateBlockedError, SendGateService
from src.email.sending import (
    SendResult,
    email_config_dict,
    imap_config_dict,
    record_successful_send_bookkeeping,
    run_post_send_hooks,
    send_workspace_snapshot,
)

UNDO_SEND_SECONDS = 30

logger = logging.getLogger(__name__)


class OutboxStateError(RuntimeError):
    pass


def queue_send_now(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    expected_revision: int,
    expected_hash: str | None = None,
    confirmed_warnings: list[str] | None = None,
    undo_seconds: int = UNDO_SEND_SECONDS,
    action_intent: dict[str, Any] | None = None,
    email_config: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    return _queue(
        conn,
        draft_id,
        actor_user_id=actor_user_id,
        expected_revision=expected_revision,
        expected_hash=expected_hash,
        confirmed_warnings=confirmed_warnings,
        undo_seconds=max(0, undo_seconds),
        scheduled_for=None,
        action_intent=action_intent,
        email_config=email_config,
        commit=commit,
    )


def queue_send_later(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    expected_revision: int,
    scheduled_for: datetime | str,
    expected_hash: str | None = None,
    confirmed_warnings: list[str] | None = None,
    email_config: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    scheduled = _as_utc(scheduled_for)
    if scheduled <= datetime.now(timezone.utc):
        raise ValueError("Scheduled send time must be in the future")
    return _queue(
        conn,
        draft_id,
        actor_user_id=actor_user_id,
        expected_revision=expected_revision,
        expected_hash=expected_hash,
        confirmed_warnings=confirmed_warnings,
        undo_seconds=0,
        scheduled_for=scheduled,
        email_config=email_config,
        commit=commit,
    )


def _queue(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    expected_revision: int,
    expected_hash: str | None,
    confirmed_warnings: list[str] | None,
    undo_seconds: int,
    scheduled_for: datetime | None,
    action_intent: dict[str, Any] | None = None,
    email_config: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    snapshot = build_workspace_snapshot(conn, draft_id, commit_initialization=commit)
    gate = SendGateService(conn, email_config).enforce(
        snapshot,
        confirmed_warnings=confirmed_warnings,
        expected_revision=expected_revision,
        expected_hash=expected_hash,
        action_intent=action_intent,
    )
    active = conn.execute(
        """SELECT * FROM draft_outbox WHERE draft_id=?
           AND status IN ('undoable','scheduled','pending','sending')
           ORDER BY created_at DESC LIMIT 1""",
        (draft_id,),
    ).fetchone()
    # A provider may already have accepted an earlier send for this draft.
    # Only the identical request may observe its previous result; a new time
    # or edited snapshot must wait for explicit operator reconciliation.
    scheduled_iso = scheduled_for.isoformat() if scheduled_for else None
    base_idempotency_key = f"draft-send:{draft_id}:{gate.snapshot_hash}:{scheduled_iso or 'now'}"
    uncertain = conn.execute(
        "SELECT * FROM draft_outbox WHERE draft_id=? AND status='uncertain' "
        "ORDER BY created_at DESC LIMIT 1",
        (draft_id,),
    ).fetchone()
    if uncertain:
        if uncertain["idempotency_key"] == base_idempotency_key:
            return _deserialize_outbox(uncertain)
        raise OutboxStateError(
            "Previous delivery outcome is unconfirmed; manual reconciliation required"
        )
    if active:
        return _deserialize_outbox(active)

    create_checkpoint(conn, draft_id, actor_user_id=actor_user_id, reason="before_schedule" if scheduled_for else "before_send", commit=False)
    outbox_id = f"dob_{uuid.uuid4().hex[:16]}"
    immutable = _copy_immutable_snapshot(snapshot, outbox_id)
    immutable["revision"]["send_affecting_hash"] = gate.snapshot_hash
    immutable["outbox_meta"] = {
        "confirmed_warnings": sorted(set(confirmed_warnings or [])),
        "queued_by": actor_user_id,
        "queued_at": utcnow_iso(),
    }
    if action_intent is not None:
        immutable["outbox_meta"]["action_id"] = action_intent["action_id"]
    undo_until = (
        datetime.now(timezone.utc) + timedelta(seconds=undo_seconds)
    ).isoformat() if undo_seconds else None
    status = "scheduled" if scheduled_for else ("undoable" if undo_seconds else "pending")
    existing_attempt = conn.execute(
        "SELECT * FROM draft_outbox WHERE idempotency_key=?",
        (base_idempotency_key,),
    ).fetchone()
    if existing_attempt and existing_attempt["status"] != "cancelled":
        return _deserialize_outbox(existing_attempt)
    idempotency_key = (
        f"{base_idempotency_key}:requeue:{outbox_id}"
        if existing_attempt
        else base_idempotency_key
    )
    conn.execute(
        """INSERT INTO draft_outbox
           (outbox_id, draft_id, snapshot_json, snapshot_hash, idempotency_key,
            status, scheduled_for, undo_until, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            outbox_id,
            draft_id,
            json.dumps(immutable, ensure_ascii=False),
            gate.snapshot_hash,
            idempotency_key,
            status,
            scheduled_iso,
            undo_until,
            actor_user_id,
            utcnow_iso(),
            utcnow_iso(),
        ),
    )
    delivery_status = "scheduled" if scheduled_for else ("undoable" if undo_seconds else "sending")
    conn.execute(
        """UPDATE drafts SET delivery_status=?, scheduled_send_at=?, status=CASE
                  WHEN status IN ('created','in_review','send_failed') THEN 'approved'
                  ELSE status END,
                  workspace_updated_at=? WHERE draft_id=?""",
        (delivery_status, scheduled_iso, utcnow_iso(), draft_id),
    )
    audit_event(
        conn,
        draft_id,
        "send_scheduled" if scheduled_for else "send_queued",
        actor_user_id,
        {"outbox_id": outbox_id, "scheduled_for": scheduled_iso, "undo_until": undo_until},
    )
    if commit:
        conn.commit()
    return get_outbox(conn, outbox_id)


def queue_legacy_scheduled_draft(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    scheduled_for: datetime,
    needs_attention: bool,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM draft_outbox WHERE draft_id=? ORDER BY created_at DESC LIMIT 1",
        (draft_id,),
    ).fetchone()
    if existing:
        return _deserialize_outbox(existing)
    snapshot = build_workspace_snapshot(conn, draft_id)
    snapshot_hash = compute_send_affecting_hash(snapshot)
    outbox_id = f"dob_{uuid.uuid4().hex[:16]}"
    immutable = _copy_immutable_snapshot(snapshot, outbox_id)
    immutable["revision"]["send_affecting_hash"] = snapshot_hash
    immutable["outbox_meta"] = {"confirmed_warnings": [], "legacy_migration": True}
    status = "needs_attention" if needs_attention else "scheduled"
    scheduled_iso = scheduled_for.astimezone(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO draft_outbox
           (outbox_id, draft_id, snapshot_json, snapshot_hash, idempotency_key,
            status, scheduled_for, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'system:migration', ?, ?)""",
        (
            outbox_id,
            draft_id,
            json.dumps(immutable, ensure_ascii=False),
            snapshot_hash,
            f"legacy-schedule:{draft_id}:{snapshot_hash}:{scheduled_iso}",
            status,
            scheduled_iso,
            utcnow_iso(),
            utcnow_iso(),
        ),
    )
    conn.execute(
        "UPDATE drafts SET delivery_status=?, scheduled_send_at=? WHERE draft_id=?",
        (status, None if needs_attention else scheduled_iso, draft_id),
    )
    return get_outbox(conn, outbox_id)


def get_outbox(conn: sqlite3.Connection, outbox_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM draft_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    if not row:
        raise ValueError("Outbox record not found")
    return _deserialize_outbox(row)


def undo_send(conn: sqlite3.Connection, outbox_id: str, *, actor_user_id: str, commit: bool = True) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM draft_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    if not row:
        raise ValueError("Outbox record not found")
    if row["status"] != "undoable" or not row["undo_until"]:
        raise OutboxStateError("Outbox record is not undoable")
    if _as_utc(row["undo_until"]) <= datetime.now(timezone.utc):
        raise OutboxStateError("Undo window has expired")
    _cancel_outbox(conn, row, actor_user_id, "send_undone", commit=commit)
    return get_outbox(conn, outbox_id)


def cancel_scheduled(conn: sqlite3.Connection, outbox_id: str, *, actor_user_id: str, commit: bool = True) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM draft_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    if not row:
        raise ValueError("Outbox record not found")
    if row["status"] not in {"scheduled", "needs_attention"}:
        raise OutboxStateError("Only scheduled delivery can be cancelled")
    _cancel_outbox(conn, row, actor_user_id, "schedule_cancelled", commit=commit)
    return get_outbox(conn, outbox_id)


def _cancel_outbox(conn: sqlite3.Connection, row: sqlite3.Row, actor_user_id: str, event_type: str, *, commit: bool = True) -> None:
    now = utcnow_iso()
    conn.execute(
        """UPDATE draft_outbox SET status='cancelled', cancelled_at=?, updated_at=?,
                  claimed_by=NULL, claim_expires_at=NULL WHERE outbox_id=?""",
        (now, now, row["outbox_id"]),
    )
    conn.execute(
        """UPDATE drafts SET delivery_status='editing', scheduled_send_at=NULL,
                  status=CASE WHEN status IN ('approved','sending','send_failed') THEN 'created' ELSE status END,
                  workspace_updated_at=? WHERE draft_id=?""",
        (now, row["draft_id"]),
    )
    audit_event(conn, row["draft_id"], event_type, actor_user_id, {"outbox_id": row["outbox_id"]})
    if commit:
        conn.commit()


def reschedule_outbox(
    conn: sqlite3.Connection,
    outbox_id: str,
    *,
    actor_user_id: str,
    scheduled_for: datetime | str,
    commit: bool = True,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM draft_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    if not row or row["status"] not in {"scheduled", "needs_attention"}:
        raise OutboxStateError("Outbox record cannot be rescheduled")
    if _has_other_uncertain(conn, row):
        raise OutboxStateError("Previous delivery outcome is unconfirmed; manual reconciliation required")
    scheduled = _as_utc(scheduled_for)
    if scheduled <= datetime.now(timezone.utc):
        raise ValueError("Scheduled send time must be in the future")
    now = utcnow_iso()
    conn.execute(
        """UPDATE draft_outbox SET status='scheduled', scheduled_for=?, last_error=NULL,
                  updated_at=? WHERE outbox_id=?""",
        (scheduled.isoformat(), now, outbox_id),
    )
    conn.execute(
        "UPDATE drafts SET delivery_status='scheduled', scheduled_send_at=?, workspace_updated_at=? WHERE draft_id=?",
        (scheduled.isoformat(), now, row["draft_id"]),
    )
    audit_event(conn, row["draft_id"], "schedule_rescheduled", actor_user_id, {"outbox_id": outbox_id, "scheduled_for": scheduled.isoformat()})
    if commit:
        conn.commit()
    return get_outbox(conn, outbox_id)


def retry_outbox(
    conn: sqlite3.Connection,
    outbox_id: str,
    *,
    actor_user_id: str,
    confirmed_warnings: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM draft_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    if not row or row["status"] not in {"failed", "needs_attention"}:
        raise OutboxStateError("Only failed or attention-required delivery can be retried")
    if _has_other_uncertain(conn, row):
        raise OutboxStateError("Previous delivery outcome is unconfirmed; manual reconciliation required")
    snapshot = json.loads(row["snapshot_json"])
    SendGateService(conn).enforce(
        snapshot,
        confirmed_warnings=confirmed_warnings or (snapshot.get("outbox_meta") or {}).get("confirmed_warnings"),
        expected_hash=row["snapshot_hash"],
    )
    conn.execute(
        """UPDATE draft_outbox SET status='pending', last_error=NULL, claimed_by=NULL,
                  claim_expires_at=NULL, updated_at=? WHERE outbox_id=?""",
        (utcnow_iso(), outbox_id),
    )
    conn.execute(
        "UPDATE drafts SET delivery_status='sending', status='approved', workspace_updated_at=? WHERE draft_id=?",
        (utcnow_iso(), row["draft_id"]),
    )
    audit_event(conn, row["draft_id"], "send_retry_queued", actor_user_id, {"outbox_id": outbox_id})
    if commit:
        conn.commit()
    return get_outbox(conn, outbox_id)


def dispatch_due_outbox(*, limit: int = 10, worker_id: str | None = None) -> dict[str, int]:
    from src.db.connection import scoped_write
    from src.governance.email_action_reconciliation import reconcile_email_action_receipts

    worker = worker_id or f"dispatcher:{uuid.uuid4().hex[:8]}"
    stats = {"claimed": 0, "sent": 0, "failed": 0, "needs_attention": 0, "uncertain": 0}
    with scoped_write() as conn:
        stats["uncertain"] += _quarantine_expired_claims(conn)
        reconcile_email_action_receipts(conn)
    for _ in range(limit):
        with scoped_write() as conn:
            row = _claim_next(conn, worker)
            if not row:
                break
            stats["claimed"] += 1
            result = dispatch_outbox(conn, row["outbox_id"], worker_id=worker)
        if result.success:
            stats["sent"] += 1
        elif result.uncertain:
            stats["uncertain"] += 1
        elif result.error and result.error.startswith("needs_attention:"):
            stats["needs_attention"] += 1
        else:
            stats["failed"] += 1
    return stats




def _has_other_uncertain(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    return conn.execute(
        "SELECT 1 FROM draft_outbox WHERE draft_id=? AND status='uncertain' "
        "AND outbox_id<>? LIMIT 1",
        (row["draft_id"], row["outbox_id"]),
    ).fetchone() is not None


def _cancel_conflicting_send(conn: sqlite3.Connection, row: sqlite3.Row) -> SendResult:
    """Retire a pre-send legacy intent without hiding the unresolved delivery."""
    now = utcnow_iso()
    changed = conn.execute(
        """UPDATE draft_outbox SET status='cancelled', cancelled_at=?, updated_at=?,
                  last_error='Prior delivery outcome unconfirmed',
                  claimed_by=NULL, claim_expires_at=NULL
           WHERE outbox_id=? AND status=?""",
        (now, now, row["outbox_id"], row["status"]),
    )
    if changed.rowcount:
        conn.execute(
            """UPDATE drafts SET delivery_status='uncertain', status='send_failed',
                      scheduled_send_at=NULL, workspace_updated_at=? WHERE draft_id=?""",
            (now, row["draft_id"]),
        )
        audit_event(
            conn, row["draft_id"], "send_conflict_cancelled", "system:outbox",
            {"outbox_id": row["outbox_id"], "reason": "prior_delivery_uncertain",
             "queued_by": row["created_by"]},
        )
        conn.commit()
    return SendResult(
        success=False, error="Prior delivery outcome unconfirmed; manual reconciliation required",
        retryable=False, uncertain=True,
    )


def dispatch_outbox(
    conn: sqlite3.Connection,
    outbox_id: str,
    *,
    worker_id: str = "inline",
    email_config: dict[str, Any] | None = None,
    imap_config: dict[str, Any] | None = None,
) -> SendResult:
    row = conn.execute("SELECT * FROM draft_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    if not row:
        return SendResult(success=False, error="Outbox record not found")
    if row["status"] == "sent":
        return SendResult(success=True, message_id=row["provider_message_id"])
    if row["status"] == "uncertain":
        return SendResult(
            success=False, error="Delivery outcome unconfirmed; manual reconciliation required",
            retryable=False, uncertain=True,
        )
    if row["status"] in {"pending", "undoable", "scheduled"} and _has_other_uncertain(conn, row):
        return _cancel_conflicting_send(conn, row)
    if row["status"] == "sending":
        if not row["claim_expires_at"] or _as_utc(row["claim_expires_at"]) <= datetime.now(timezone.utc):
            _quarantine_expired_claims(conn)
            return SendResult(
                success=False, error="Delivery claim expired; manual reconciliation required",
                retryable=False, uncertain=True,
            )
        if row["claimed_by"] != worker_id:
            return SendResult(
                success=False, error="Outbox claimed by another worker", retryable=False,
            )
        if _has_other_uncertain(conn, row):
            return SendResult(
                success=False, error="Prior delivery outcome unconfirmed; manual reconciliation required",
                retryable=False, uncertain=True,
            )
    else:
        claimed = _claim_specific(conn, row, worker_id)
        if not claimed:
            return SendResult(success=False, error=f"Outbox record is not dispatchable from {row['status']}")
        row = claimed

    snapshot = json.loads(row["snapshot_json"])
    effective_email_config = email_config or email_config_dict(CONFIG)
    effective_imap_config = imap_config if imap_config is not None else imap_config_dict(CONFIG)
    try:
        current = build_workspace_snapshot(conn, row["draft_id"])
        if compute_send_affecting_hash(current) != row["snapshot_hash"]:
            return _mark_needs_attention(conn, row, "Draft changed after delivery was queued")
        SendGateService(conn, effective_email_config).enforce(
            snapshot,
            confirmed_warnings=(snapshot.get("outbox_meta") or {}).get("confirmed_warnings"),
            expected_hash=row["snapshot_hash"],
        )
    except (SendGateBlockedError, ValueError) as exc:
        detail = exc.result.to_dict() if isinstance(exc, SendGateBlockedError) else str(exc)
        return _mark_needs_attention(conn, row, json.dumps(detail, ensure_ascii=False))

    result = send_workspace_snapshot(
        conn,
        snapshot,
        effective_email_config,
        imap_config=effective_imap_config,
    )
    now = utcnow_iso()
    attempts = result.attempts if isinstance(result.attempts, int) else 1
    if result.success:
        message_id = result.message_id or f"outbox:{outbox_id}"
        conn.execute(
            """UPDATE draft_outbox SET status='sent', provider_message_id=?,
                      provider_result_json=?, sent_at=?, updated_at=?,
                      claimed_by=NULL, claim_expires_at=NULL WHERE outbox_id=?""",
            (message_id, json.dumps({"success": True, "attempts": attempts}), now, now, outbox_id),
        )
        conn.execute(
            """UPDATE drafts SET delivery_status='sent', status='sent', sent_at=?,
                      sent_version=?, scheduled_send_at=NULL, workspace_updated_at=?
               WHERE draft_id=?""",
            (now, str((snapshot.get("revision") or {}).get("number") or 1), now, row["draft_id"]),
        )
        audit_event(conn, row["draft_id"], "send_completed", row["created_by"], {"outbox_id": outbox_id, "message_id": message_id})
        # Persist provider acceptance before any non-essential bookkeeping. A
        # later local failure must never make the dispatcher retry a message
        # that the provider may already have accepted.
        conn.commit()

        envelope = normalize_envelope(snapshot.get("envelope"))
        to_email, _cc, _bcc = envelope_to_legacy(envelope)
        identity_id = (snapshot.get("envelope") or {}).get("sender_identity_id")
        identity = next(
            (
                item
                for item in effective_email_config.get("sender_identities") or []
                if item.get("identity_id") == identity_id
            ),
            None,
        )
        savepoint = "successful_send_bookkeeping"
        try:
            conn.execute(f"SAVEPOINT {savepoint}")
            record_successful_send_bookkeeping(
                conn,
                draft_id=row["draft_id"],
                message_id=message_id,
                to_email=to_email or "",
                subject=(snapshot.get("content") or {}).get("subject") or "",
                snapshot=snapshot,
                sender_email=(identity or {}).get("email_address") or effective_email_config.get("from_email"),
                commit=False,
            )
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            conn.commit()
        except Exception as exc:
            logger.exception("Provider accepted outbox %s but local send bookkeeping failed", outbox_id)
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error:
                conn.rollback()
            audit_event(
                conn,
                row["draft_id"],
                "send_bookkeeping_warning",
                row["created_by"],
                {"outbox_id": outbox_id, "error": str(exc)},
            )
            conn.commit()
        run_post_send_hooks(
            conn,
            draft_id=row["draft_id"],
            to_email=to_email or "",
        )
        return result

    if result.uncertain:
        conn.execute(
            """UPDATE draft_outbox SET status='uncertain', last_error=?, provider_result_json=?,
                      updated_at=?, claimed_by=NULL, claim_expires_at=NULL WHERE outbox_id=?""",
            (
                result.error,
                json.dumps({"success": False, "outcome": "uncertain", "attempts": attempts,
                            "recipient_rejections": result.recipient_rejections}),
                now, outbox_id,
            ),
        )
        conn.execute(
            """UPDATE drafts SET delivery_status='uncertain', status='send_failed',
                      scheduled_send_at=NULL, workspace_updated_at=? WHERE draft_id=?""",
            (now, row["draft_id"]),
        )
        audit_event(
            conn, row["draft_id"], "send_uncertain", row["created_by"],
            {"outbox_id": outbox_id, "reason": result.error or "provider_acceptance_unknown"},
        )
        conn.commit()
        return result

    conn.execute(
        """UPDATE draft_outbox SET status='failed', last_error=?, provider_result_json=?,
                  updated_at=?, claimed_by=NULL, claim_expires_at=NULL WHERE outbox_id=?""",
        (result.error, json.dumps({"success": False, "attempts": attempts}), now, outbox_id),
    )
    conn.execute(
        "UPDATE drafts SET delivery_status='failed', status='send_failed', workspace_updated_at=? WHERE draft_id=?",
        (now, row["draft_id"]),
    )
    audit_event(conn, row["draft_id"], "send_failed", row["created_by"], {"outbox_id": outbox_id, "error": result.error})
    conn.commit()
    return result
