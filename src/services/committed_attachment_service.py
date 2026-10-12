"""Journal an attachment added after its draft has already committed."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, ContextManager

from src.email.attachment_recovery import (
    AttachmentCleanupError, abandon_upload_collision, apply_file_change,
    attachment_visibility_lock, mark_unresolved, prepare_upload,
    record_committed_decision, recover_pending, require_reconciled,
)
from src.email.attachments import AttachmentError, stage_attachment
from src.events.bus import Event, event_bus

logger = logging.getLogger(__name__)
WriteScope = Callable[[], ContextManager[sqlite3.Connection]]


@contextmanager
def explicit_attachment_write_scope(conn: sqlite3.Connection):
    """Own one command on an idle explicit connection, without prior writes."""
    if conn.in_transaction:
        raise ValueError("Attachment command requires an idle explicit connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def add_committed_draft_attachment(
    write_scope: WriteScope, *, data_dir: str, draft_id: str,
    filename: str, data: bytes, actor_user_id: str,
    lease_token: str | None = None,
) -> dict:
    """Commit one receipt or reconcile its rollback before returning.

    A scope owns commit/rollback. A commit that succeeds then raises has an
    unknown caller outcome: recovery follows the durable receipt, then the
    exception propagates. The caller must not blindly retry it.
    """
    from src.email.workspace_support import (
        prepare_attachment_mutation, refresh_attachment_revision,
    )

    require_reconciled()
    intent = None
    digest = hashlib.sha256(data).hexdigest()
    with attachment_visibility_lock(data_dir):
        staged = stage_attachment(data_dir, filename, data)
        try:
            with write_scope() as conn:
                recover_pending(conn, data_dir)
                if not conn.execute(
                    "SELECT 1 FROM drafts WHERE draft_id=?", (draft_id,),
                ).fetchone():
                    raise ValueError("Draft not found")
                before = prepare_attachment_mutation(
                    conn, draft_id, expected_revision=None,
                    actor_user_id=actor_user_id, lease_token=lease_token,
                    attachment_data_dir=data_dir, initialize=False,
                )
                for _ in range(10000):
                    intent = prepare_upload(
                        data_dir, draft_id, filename, staged, len(data), digest,
                    )
                    try:
                        apply_file_change(data_dir, intent)
                    except FileExistsError:
                        abandon_upload_collision(data_dir, intent)
                        intent = None
                        continue
                    break
                else:
                    raise AttachmentError("Too many attachment name collisions")
                refresh_attachment_revision(
                    conn, draft_id, expected_revision=before["revision"]["number"],
                    actor_user_id=actor_user_id, lease_token=lease_token,
                    attachment_data_dir=data_dir, commit=False,
                    return_workspace=False,
                )
                record_committed_decision(conn, intent)
        except BaseException:
            if intent is not None:
                try:
                    with write_scope() as recovery_conn:
                        recover_pending(recovery_conn, data_dir)
                except BaseException:
                    mark_unresolved()
                    logger.exception("Attachment compensation failed; restart recovery required")
                    raise
            else:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    logger.exception("Failed to clean unpublished attachment stage")
            raise

        try:
            with write_scope() as cleanup_conn:
                recover_pending(cleanup_conn, data_dir)
        except AttachmentCleanupError:
            logger.exception("Committed attachment cleanup deferred to restart")
        except BaseException:
            mark_unresolved()
            logger.exception("Committed attachment outcome requires recovery")
            raise

    try:
        event_bus.publish(Event(
            event_type="draft_attachment_added",
            detail={"draft_id": draft_id, "filename": intent.filename},
            surface="system",
        ))
    except Exception:
        logger.exception("Committed attachment event publication failed")

    return {
        "filename": intent.filename,
        "size": len(data),
        "path": str(Path(data_dir) / "attachments" / draft_id / intent.filename),
    }
