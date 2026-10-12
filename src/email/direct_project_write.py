"""One transaction for a policy-authorized direct email project update."""

import logging
import sqlite3
from collections.abc import Callable
from uuid import uuid4

from src.email.inbox_fetcher import FetchedEmail
from src.events.bus import Event, event_bus
from src.repositories.activity_log_repo import insert_activity
from src.repositories.evidence_repo import insert_evidence
from src.write_layer.models import WriteResult

logger = logging.getLogger(__name__)


def apply_direct_project_update(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    fields: dict,
    actor: str,
    reason: str,
    evidence_ref: str,
    confidence: float,
    fetched: FetchedEmail | None = None,
    post_commit: list[Callable[[], None]] | None = None,
) -> WriteResult:
    """Write guarded fields and companion records within the owning transaction.

    The route has already checked the confirmation/confidence policy. A savepoint
    limits a failed command to its own writes when the caller supplied a wider
    transaction. Only an owned transaction is committed here; callers must
    dispatch ``post_commit`` after successfully committing their own transaction.
    """
    from src.write_layer.write_helpers import safe_write_project

    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    savepoint = "email_direct_" + uuid4().hex
    try:
        conn.execute("SAVEPOINT " + savepoint)
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise
    savepoint_open = True
    try:
        result = safe_write_project(
            conn, project_id, fields, actor=actor, reason=reason,
            evidence_ref=evidence_ref, surface="email", commit=False,
            publish_event=False, schedule_notifications=False,
            rollback_on_error=False,
        )
        if result.outcome != "accepted":
            # A needs_review audit without a proposal must not look like a
            # queued review. Rejection/no-op also has no direct command effect.
            conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            conn.execute("RELEASE SAVEPOINT " + savepoint)
            savepoint_open = False
            if owns_transaction:
                conn.rollback()
            return result

        fields_desc = ", ".join(f"{key}={value}" for key, value in fields.items())
        insert_activity(
            conn, actor=actor, action_type="auto_truth_write",
            project_id=project_id, surface="email",
            summary=(f"Auto-updated {fields_desc} "
                     f"from email evidence (conf={confidence:.2f})"),
        )
        if fetched is not None:
            insert_evidence(
                conn, project_id=project_id, evidence_type="email",
                content_summary=(
                    f"Email from {fetched.sender_email}: "
                    f"{fetched.subject[:120] if fetched.subject else '(no subject)'}. "
                    f"Triggered: {fields_desc}"
                ),
                evidence_at=fetched.received_at, source_ref=evidence_ref,
            )
        conn.execute("RELEASE SAVEPOINT " + savepoint)
        savepoint_open = False
        if owns_transaction:
            conn.commit()
    except Exception:
        if savepoint_open:
            conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            conn.execute("RELEASE SAVEPOINT " + savepoint)
            if owns_transaction:
                conn.rollback()
        else:
            # Only an owned commit failure may clear the whole transaction.
            # Never roll back unrelated work on a borrowed connection.
            if owns_transaction:
                conn.rollback()
        raise

    def _publish_update() -> None:
        try:
            event_bus.publish(Event(
                event_type="project_updated", project_id=project_id,
                detail={"fields": list(result.applied_fields)}, surface="email",
            ))
        except Exception:
            # A failed refresh cannot turn a durable write into a retryable failure.
            logger.exception("Post-commit direct email update event failed for %s", project_id)

    if owns_transaction:
        _publish_update()
    elif post_commit is not None:
        post_commit.append(_publish_update)
    return result
