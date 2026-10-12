"""Committed draft approval command for synchronous compatibility surfaces.

The approval transition and immutable delivery intent are committed together.
Provider delivery is a separate step and must never run on a draft whose intent
failed to commit. Callers own identity/authorization and presentation.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from src.email.draft_send_command import queue_approved_draft_send
from src.email.draft_session import get_draft, update_draft_status
from src.email.outbox import dispatch_outbox

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftApprovalReceipt:
    state: str  # not_queued, queued, in_progress, sent, failed, uncertain
    error: str | None = None
    message_id: str | None = None
    outbox_id: str | None = None


def commit_immediate_draft_approval(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    email_config: dict[str, Any],
    scope_start_changes: int,
) -> DraftApprovalReceipt:
    """Commit status/history and outbox as one caller-owned write command.

    ``scope_start_changes`` is captured by the adapter when it opens its own
    clean writer scope. Refuse to commit another command's pending writes.
    """
    if conn.total_changes != scope_start_changes:
        return DraftApprovalReceipt("not_queued", "Write scope has pending changes")
    try:
        draft = get_draft(conn, draft_id)
        if draft is None or draft["status"] not in {"created", "in_review"}:
            return DraftApprovalReceipt("not_queued", "Draft is not awaiting approval")
        if draft["status"] == "created" and not update_draft_status(
            conn, draft_id, "in_review", commit=False,
        ):
            conn.rollback()
            return DraftApprovalReceipt("not_queued", "Draft review transition failed")
        if not update_draft_status(conn, draft_id, "approved", commit=False):
            conn.rollback()
            return DraftApprovalReceipt("not_queued", "Draft approval transition failed")
        queued, rejection = queue_approved_draft_send(
            conn, draft_id, email_config, actor_user_id=actor_user_id, commit=False,
        )
        if rejection is not None:
            conn.rollback()
            return DraftApprovalReceipt("not_queued", rejection.error)
        assert queued is not None
        conn.commit()
        return DraftApprovalReceipt("queued", outbox_id=queued["outbox_id"])
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        logger.exception("Could not commit draft approval intent for %s", draft_id)
        # A commit-then-raise may already be durable; never send or invite a
        # blind retry based on this in-memory exception alone.
        return DraftApprovalReceipt("uncertain", f"Approval/queue outcome unconfirmed: {exc}")


def dispatch_committed_draft_approval(
    conn: sqlite3.Connection,
    receipt: DraftApprovalReceipt,
    *,
    email_config: dict[str, Any],
    imap_config: dict[str, Any] | None,
) -> DraftApprovalReceipt:
    """Dispatch only a committed outbox ID and report its durable outcome."""
    if receipt.state != "queued" or receipt.outbox_id is None:
        return receipt
    if conn.in_transaction:
        # Outbox claim/receipt code commits its connection. Never let an
        # independent caller's pending changes ride along with delivery.
        return DraftApprovalReceipt(
            "queued", "Immediate dispatch deferred: caller transaction is active",
            outbox_id=receipt.outbox_id,
        )
    dispatch_error = None
    try:
        result = dispatch_outbox(
            conn, receipt.outbox_id, email_config=email_config, imap_config=imap_config,
        )
    except Exception as exc:
        logger.exception("Draft dispatch raised after queue commit: %s", receipt.outbox_id)
        dispatch_error = exc
        result = None
        if conn.in_transaction:
            # The provider may have accepted a message before the local send
            # receipt failed. Discard uncommitted "sent" writes before reading
            # the durable claim and deciding whether it needs quarantine.
            try:
                conn.rollback()
            except Exception:
                logger.exception("Could not roll back failed draft receipt: %s", receipt.outbox_id)
                # Do not return normally with a dirty transaction: a caller's
                # scope could then commit the unverified success receipt.
                conn.execute("ROLLBACK")
    row = conn.execute(
        "SELECT status, claimed_by, draft_id, provider_message_id, last_error "
        "FROM draft_outbox WHERE outbox_id=?",
        (receipt.outbox_id,),
    ).fetchone()
    if row is not None:
        if row["status"] == "sent":
            return DraftApprovalReceipt("sent", message_id=row["provider_message_id"], outbox_id=receipt.outbox_id)
        if row["status"] in {"pending", "undoable", "scheduled"}:
            return DraftApprovalReceipt("queued", outbox_id=receipt.outbox_id)
        if row["status"] == "sending":
            if dispatch_error is not None and row["claimed_by"] == "inline":
                # The synchronous dispatch returned by exception after claiming.
                # Acceptance may have occurred: quarantine its exact claim, not
                # another worker's competing receipt, and never replay it.
                error = f"Delivery outcome unconfirmed after dispatch exception: {dispatch_error}"
                updated = conn.execute(
                    "UPDATE draft_outbox SET status='uncertain', last_error=?, "
                    "claimed_by=NULL, claim_expires_at=NULL, updated_at=datetime('now') "
                    "WHERE outbox_id=? AND status='sending' AND claimed_by='inline'",
                    (error, receipt.outbox_id),
                )
                if updated.rowcount:
                    conn.execute(
                        "UPDATE drafts SET status='send_failed', delivery_status='uncertain' "
                        "WHERE draft_id=? AND status='sending'",
                        (row["draft_id"],),
                    )
                    conn.commit()
                    return DraftApprovalReceipt("uncertain", error, outbox_id=receipt.outbox_id)
                conn.rollback()
                return DraftApprovalReceipt("uncertain", error, outbox_id=receipt.outbox_id)
            return DraftApprovalReceipt("in_progress", outbox_id=receipt.outbox_id)
        if row["status"] in {"uncertain", "cancelled"}:
            return DraftApprovalReceipt("uncertain", row["last_error"], outbox_id=receipt.outbox_id)
        if row["status"] in {"failed", "needs_attention"}:
            return DraftApprovalReceipt("failed", row["last_error"] or (result.error if result else None), outbox_id=receipt.outbox_id)
    return DraftApprovalReceipt("uncertain", "Delivery receipt unavailable; manual reconciliation required", outbox_id=receipt.outbox_id)
