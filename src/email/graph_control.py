"""Explicit transaction starts for dedicated Graph acquisition/control owners."""
from __future__ import annotations

from functools import wraps
import logging
import sqlite3

logger = logging.getLogger(__name__)


def begin_graph_control(conn: sqlite3.Connection) -> None:
    """Keep control SQL staged until its existing owner acknowledges COMMIT.

    Dedicated Graph connections may use autocommit. DML and a top-level
    SAVEPOINT must not become durable ahead of the explicit owner seam. If
    a FIX bridge has already opened a transaction, retain it through receipt.
    The Graph poll entry rejects borrowed caller transactions before any work.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN")


def dedicated_graph_poll(poll):
    """Dispose failed dedicated work without touching a borrowed transaction.

    Validate ownership before entering the cleanup region. Rollback discards
    only still-uncommitted work; it cannot undo an acknowledged or unknown
    durable COMMIT. Preserve the primary failure even if cleanup itself fails.
    """
    @wraps(poll)
    def guarded(conn: sqlite3.Connection, *args, **kwargs):
        if conn.in_transaction:
            raise ValueError("Graph poll requires an idle dedicated connection")
        try:
            return poll(conn, *args, **kwargs)
        except BaseException:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except BaseException:
                logger.exception("dedicated Graph poll rollback failed; connection state unknown")
            raise

    return guarded
