"""Commit an outreach ticket, draft and optional attachment as one command."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from src.db.savepoint import savepoint
from src.email.attachment_recovery import (
    AttachmentCleanupError, abandon_upload_collision, apply_file_change,
    attachment_visibility_lock,
    mark_unresolved, prepare_upload, record_committed_decision, recover_pending,
    require_reconciled,
)
from src.email.attachments import stage_attachment
from src.email.draft_session import create_draft
from src.repositories.ticket_repo import create_ticket


class OutreachDraftReceipt(BaseModel):
    draft_id: str
    ticket_id: str
    attachment_filename: str | None = None


def create_outreach_draft(
    conn: sqlite3.Connection, *, to_email: str, counterparty: str,
    subject: str, body: str, cc_list: list[str], data_dir: str,
    doc_filename: str | None = None, doc_path: Path | None = None,
) -> OutreachDraftReceipt:
    """Own the explicit connection's commit and reconcile file/DB decisions."""
    with attachment_visibility_lock(data_dir):
        if (doc_filename is None) != (doc_path is None):
            raise ValueError("Outreach document filename and path must be supplied together")
        # The draft, ticket, attachment decision and workspace initialization
        # must commit together. Stage bytes before touching the database.
        require_reconciled()
        staged: Path | None = None
        intent = None
        post_commit = []
        if doc_filename is not None and doc_path is not None:
            data = doc_path.read_bytes()
            staged = stage_attachment(data_dir, doc_filename, data)
        try:
            with savepoint(conn, ensure_transaction=True):
                if staged is not None:
                    recover_pending(conn, data_dir)
                ticket = create_ticket(
                    conn,
                    title=f"Outreach: {counterparty} - {subject[:60]}",
                    category="outreach",
                    severity="low",
                    description=body[:1000],
                    counterparty=counterparty,
                    reporter_email=to_email,
                    source_type="manual",
                )
                cc_str = ", ".join(cc_list) if cc_list else None
                draft_id = create_draft(
                    conn,
                    project_id=None,
                    task_type="outreach",
                    subject=subject,
                    body=body,
                    to_email=to_email,
                    cc=cc_str,
                    ticket_id=ticket.ticket_id,
                    commit=False,
                    post_commit=post_commit,
                    attachment_data_dir=data_dir,
                )
                if staged is not None:
                    for _ in range(10000):
                        intent = prepare_upload(
                            data_dir, draft_id, doc_filename, staged,
                            len(data), hashlib.sha256(data).hexdigest(),
                        )
                        try:
                            apply_file_change(data_dir, intent)
                        except FileExistsError:
                            abandon_upload_collision(data_dir, intent)
                            intent = None
                            continue
                        break
                    else:
                        raise RuntimeError("Too many attachment name collisions")
                    from src.email.workspace_support import refresh_attachment_revision

                    revision = conn.execute(
                        "SELECT workspace_revision FROM drafts WHERE draft_id=?", (draft_id,),
                    ).fetchone()[0]
                    refresh_attachment_revision(
                        conn, draft_id, expected_revision=revision,
                        actor_user_id="system:outreach", attachment_data_dir=data_dir,
                        commit=False, return_workspace=False,
                    )
                    record_committed_decision(conn, intent)
            conn.commit()
        except BaseException:
            # If commit failed, its outcome can be uncertain; discard the local
            # transaction before using the durable receipt as the decision.
            try:
                conn.rollback()
            except BaseException:
                mark_unresolved()
                logging.getLogger(__name__).exception(
                    "Outreach rollback failed; attachment state requires restart recovery"
                )
                raise
            if intent is not None:
                try:
                    recover_pending(conn, data_dir)
                except BaseException:
                    mark_unresolved()
                    logging.getLogger(__name__).exception(
                        "Outreach attachment compensation failed; restart recovery required"
                    )
                    raise
            elif staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).exception("Failed to clean staged outreach attachment")
            raise
        if intent is not None:
            try:
                recover_pending(conn, data_dir)
            except AttachmentCleanupError:
                logging.getLogger(__name__).exception(
                    "Committed outreach attachment cleanup deferred to restart"
                )
            except BaseException:
                mark_unresolved()
                raise
    for effect in post_commit:
        effect()

    return OutreachDraftReceipt(
        draft_id=draft_id,
        ticket_id=ticket.ticket_id,
        attachment_filename=intent.filename if intent is not None else None,
    )
