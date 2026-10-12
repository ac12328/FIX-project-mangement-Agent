"""Commit review-card delivery intent with a proposal before Telegram I/O."""

import logging
import sqlite3
from collections.abc import Callable

logger = logging.getLogger(__name__)


def queue_review_notification(
    conn: sqlite3.Connection, *, update_id: str, project_id: str, payload: dict,
    store_proposal: Callable[[], None],
) -> str:
    """Enqueue a proposal and bound intent without committing caller-owned work.

    A caller that already has a transaction must commit it itself. Its outbox
    row is then picked up by the notification worker; no card is sent before
    that durability boundary. An idle connection owns this local command and
    may commit and dispatch synchronously for legacy direct callers.
    """
    from src.email.update_notification_outbox import (
        deliver_update_notification, enqueue_update_notification,
    )
    from src.telegram.review_push import push_review_card_sync

    # An outermost SAVEPOINT commits on RELEASE in SQLite. Open a real
    # transaction first so the explicit commit is the durability boundary.
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT review_notification")
    try:
        store_proposal()
        enqueue_update_notification(
            conn, update_id=update_id, project_id=project_id,
            payload={**payload, "_notification_kind": "review_card"},
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT review_notification")
        conn.execute("RELEASE SAVEPOINT review_notification")
        if owns_transaction:
            conn.rollback()
        raise
    conn.execute("RELEASE SAVEPOINT review_notification")
    if not owns_transaction:
        return "review_notification_pending"
    try:
        conn.commit()
    except Exception:
        # A failed commit cannot safely be treated as a durable delivery intent.
        conn.rollback()
        raise
    try:
        outcome = deliver_update_notification(
            update_id, conn=conn, sender=push_review_card_sync,
        )
    except Exception:
        # The committed intent survives. The scheduler can claim pending work;
        # an unconfirmed send must be reconciled rather than replayed blindly.
        try:
            logger.exception("Review notification dispatch failed for %s", update_id)
        except Exception:
            # Even a broken logging handler cannot reclassify committed work.
            pass
        return "review_notification_unknown"
    return outcome


