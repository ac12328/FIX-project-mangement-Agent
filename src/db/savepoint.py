"""Nest one command under a caller-owned SQLite transaction."""

from contextlib import contextmanager
import sqlite3
from uuid import uuid4
from typing import Iterator


@contextmanager
def savepoint(
    conn: sqlite3.Connection, *, ensure_transaction: bool = False
) -> Iterator[None]:
    """Discard command writes without rolling back prior caller work.

    With ``ensure_transaction=True``, an idle autocommit connection begins a
    transaction first. Otherwise releasing the outermost SQLite savepoint
    would commit a command that the caller requested with ``commit=False``.
    The caller still owns commit/rollback after a successful return.
    """
    started_transaction = ensure_transaction and not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    name = "command_" + uuid4().hex
    conn.execute("SAVEPOINT " + name)
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT " + name)
        conn.execute("RELEASE SAVEPOINT " + name)
        if started_transaction:
            conn.rollback()
        raise
    else:
        conn.execute("RELEASE SAVEPOINT " + name)
