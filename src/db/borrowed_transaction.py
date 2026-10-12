"""Restrict accidental transaction ownership in a nested SQLite command."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from uuid import uuid4


def _reject_transaction_control(sql: str) -> None:
    command = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if command in {"COMMIT", "END", "BEGIN"} or (
        command == "ROLLBACK" and not sql.lstrip().upper().startswith("ROLLBACK TO ")
    ):
        raise RuntimeError("The caller owns this transaction")


class _BorrowedCursor:
    """Keep cursor-level SQL from closing the caller's transaction."""

    def __init__(self, cursor: Any):
        self._cursor = cursor

    def execute(self, sql: str, *args: Any) -> _BorrowedCursor:
        _reject_transaction_control(sql)
        self._cursor.execute(sql, *args)
        return self

    def executemany(self, sql: str, *args: Any) -> _BorrowedCursor:
        _reject_transaction_control(sql)
        self._cursor.executemany(sql, *args)
        return self

    def executescript(self, _script: str) -> None:
        raise RuntimeError("SQL scripts implicitly commit the caller's transaction")

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        if name == "connection":
            raise AttributeError(name)
        return getattr(self._cursor, name)


class BorrowedTransactionConnection:
    """Present the route with SQL access but not outer transaction ownership."""

    def __init__(self, conn: Any):
        self._conn = conn

    def execute(self, sql: str, *args: Any) -> _BorrowedCursor:
        _reject_transaction_control(sql)
        return _BorrowedCursor(self._conn.execute(sql, *args))

    def executemany(self, sql: str, *args: Any) -> _BorrowedCursor:
        _reject_transaction_control(sql)
        return _BorrowedCursor(self._conn.executemany(sql, *args))

    def cursor(self, *args: Any, **kwargs: Any) -> _BorrowedCursor:
        return _BorrowedCursor(self._conn.cursor(*args, **kwargs))

    def executescript(self, _script: str) -> None:
        raise RuntimeError("SQL scripts implicitly commit the caller's transaction")

    def commit(self) -> None:
        raise RuntimeError("The caller owns this transaction")

    def rollback(self) -> None:
        raise RuntimeError("The caller owns this transaction")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)




class _SavepointCommandConnection(BorrowedTransactionConnection):
    """Allow checkpointing within, but never closing, a caller's transaction."""

    def __init__(self, conn: Any, savepoint_name: str):
        super().__init__(conn)
        self._savepoint_name = savepoint_name

    def commit(self) -> None:
        # Durable commit belongs to the outer write scope.
        return None

    def rollback(self) -> None:
        # Discard only this command's writes, not preceding caller changes.
        self._conn.execute("ROLLBACK TO SAVEPOINT " + self._savepoint_name)


@contextmanager
def borrowed_command_savepoint(conn: Any) -> Iterator[_SavepointCommandConnection]:
    """Keep a checkpointing subcommand inside an existing caller transaction."""
    if not conn.in_transaction:
        raise RuntimeError("A borrowed command requires a caller-owned transaction")
    name = "borrowed_command_" + uuid4().hex
    conn.execute("SAVEPOINT " + name)
    try:
        yield _SavepointCommandConnection(conn, name)
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT " + name)
        raise
    finally:
        conn.execute("RELEASE SAVEPOINT " + name)
