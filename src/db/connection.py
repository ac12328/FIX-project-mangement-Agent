"""
Database connection manager for AgentFIXBPRO.
WAL mode, thread-local connections, foreign_keys ON, row_factory=sqlite3.Row.
All functions accept a `conn` parameter for testability.

WriteSerializer + scoped_write():  Single-connection serialized write access.
One connection + one threading.Lock.  All writers acquire the lock, run inside
BEGIN IMMEDIATE / COMMIT, and release.  No retry loops — just a timeout on
lock acquisition (default 60s).
"""

import inspect
import logging
import sqlite3
import threading
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)


@contextmanager
def _detached_span(tracer, name: str):
    """End a span without attaching context across caller-owned scopes."""
    span = tracer.start_span(name)
    try:
        yield span
    finally:
        span.end()


_thread_local = threading.local()


def get_connection(db_path: str | Path = None) -> sqlite3.Connection:
    """
    Get a thread-local SQLite connection.
    If db_path is ":memory:", creates a non-thread-local in-memory DB (for tests).
    """
    warnings.warn(
        "get_connection() is deprecated. Use scoped_read() for reads or "
        "scoped_write() for writes. See WS-1 design spec.",
        DeprecationWarning,
        stacklevel=2,
    )
    if db_path == ":memory:":
        conn = sqlite3.connect(":memory:")
        _configure_sqlite_connection(conn, write=True)
        return conn

    if db_path is None:
        from config.settings import DB_PATH
        db_path = str(DB_PATH)
    else:
        db_path = str(db_path)

    if not hasattr(_thread_local, "connections"):
        _thread_local.connections = {}

    if db_path not in _thread_local.connections:
        conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        _configure_sqlite_connection(conn, write=True)
        _thread_local.connections[db_path] = conn
    else:
        # Liveness check — replace closed connections
        conn = _thread_local.connections[db_path]
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            logger.warning("Replacing closed connection for %s", db_path)
            conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
            _configure_sqlite_connection(conn, write=True)
            _thread_local.connections[db_path] = conn

    return _thread_local.connections[db_path]


def _configure_sqlite_connection(conn: sqlite3.Connection, *, write: bool = False) -> None:
    """Apply standard SQLite pragmas on every new connection."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    if write:
        # Journal mode is database-wide. Reapplying it on concurrent readers can
        # require a lock and make otherwise read-only connection setup fail.
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if not write:
        conn.execute("PRAGMA query_only = ON")


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Deprecated alias — use _configure_sqlite_connection instead."""
    _configure_sqlite_connection(conn, write=True)


def open_connection(db_path: str | Path = None) -> sqlite3.Connection:
    """Open a new configured SQLite connection.

    Unlike get_connection(), this does not use thread-local caching — the
    caller owns the connection lifetime and must close it on shutdown.
    Use for long-lived read-only connections where the caller manages the
    lifecycle explicitly. Writes must use scoped_write().
    """
    if db_path is None:
        from config.settings import DB_PATH
        db_path = str(DB_PATH)
    else:
        db_path = str(db_path)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.isolation_level = None  # autocommit — explicit BEGIN only via scoped_write
    _configure_sqlite_connection(conn, write=False)
    return conn


def checkpoint_wal(conn: sqlite3.Connection) -> None:
    """Force WAL checkpoint — flush all WAL data back to main DB file."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.info("WAL checkpoint completed")
    except sqlite3.OperationalError as e:
        logger.warning("WAL checkpoint failed: %s", e)


def check_integrity(conn: sqlite3.Connection) -> bool:
    """Run PRAGMA integrity_check. Returns True if DB is healthy.

    Checkpoints the WAL first (passive — non-blocking) so the check sees
    a consistent view of the database rather than a mid-write WAL state.
    """
    try:
        # Flush WAL pages to the main file before inspecting
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        result = conn.execute("PRAGMA integrity_check").fetchone()
        ok = result is not None and result[0] == "ok"
        if not ok:
            logger.error("Database integrity check FAILED: %s", result)
        return ok
    except sqlite3.DatabaseError as e:
        logger.error("Database integrity check error: %s", e)
        return False


def close_connection(db_path: str | Path = None) -> None:
    """Close the thread-local connection for a given db_path (with WAL checkpoint)."""
    if not hasattr(_thread_local, "connections"):
        return
    if db_path is None:
        from config.settings import DB_PATH
        db_path = str(DB_PATH)
    else:
        db_path = str(db_path)

    conn = _thread_local.connections.pop(db_path, None)
    if conn:
        try:
            checkpoint_wal(conn)
        except Exception:
            pass
        conn.close()


def close_all_connections() -> None:
    """Close all thread-local connections (with WAL checkpoint). Call on shutdown."""
    if not hasattr(_thread_local, "connections"):
        return
    for db_path in list(_thread_local.connections.keys()):
        conn = _thread_local.connections.pop(db_path, None)
        if conn:
            try:
                checkpoint_wal(conn)
            except Exception:
                pass
            conn.close()
    logger.info("All DB connections closed")


def _scoped_write_caller_hint() -> str:
    """Best-effort caller without materializing the full Python stack."""
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame is not None else None
        while frame is not None:
            filename = frame.f_code.co_filename
            function = frame.f_code.co_name
            if not (
                (filename.endswith("connection.py") and function in {
                    "connection", "scoped_write", "scoped_observability_write",
                    "__enter__", "__exit__",
                })
                or "contextlib.py" in filename
            ):
                return f"{Path(filename).name}:{function}:{frame.f_lineno}"
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


# ── WriteSerializer: single-connection serialized write access ──────────


@dataclass
class _WriteLease:
    """Ownership belongs to one scope, including while it is suspended for I/O."""

    acquired: bool = False
    failed: bool = False
    busy_timeout_ms: int | None = None


class WriteSerializer:
    """Single-connection serialized write access to SQLite.

    One connection + one ``threading.Lock``.  All writers acquire the lock,
    run inside ``BEGIN IMMEDIATE`` / ``COMMIT``, and release.  No retry
    loops, no convoy effect — just a timeout on lock acquisition.

    This replaces the former ``WritePool`` which used multiple connections
    and a jitter-retry loop, leading to 155-second cascade failures under
    contention.
    """

    _DEFAULT_TIMEOUT = 60  # seconds

    def __init__(self, db_path: str | Path, *, timeout: int = _DEFAULT_TIMEOUT):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        # Leases belong to logical scopes, not OS threads: sync API dependency
        # entry/body/exit can execute in different worker-pool threads.
        self._current_lease: _WriteLease | None = None
        self._timeout = timeout
        self._conn: sqlite3.Connection = self._make_conn()

    def _make_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, check_same_thread=False)
        _configure_sqlite_connection(conn, write=True)
        # Manual transaction control — we use explicit BEGIN IMMEDIATE
        conn.isolation_level = None
        return conn

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[sqlite3.Connection]:
        """Acquire the write lock and yield a transactional connection.

        ``timeout`` is local to this acquisition. It never mutates the shared
        serializer timeout, which keeps concurrent best-effort observability
        writes from changing business-write behaviour.
        """
        effective_timeout = self._timeout if timeout is None else max(0.0, float(timeout))
        caller = _scoped_write_caller_hint()
        tracer = trace.get_tracer("agentfixbpro.db")
        wait_start = time.perf_counter()
        lease = _WriteLease(
            busy_timeout_ms=max(1, int(effective_timeout * 1000)) if timeout is not None else None,
        )
        conn: sqlite3.Connection | None = None
        restore_busy_timeout = timeout is not None
        with _detached_span(tracer, "db.write") as span:
            span.set_attribute("db.system", "sqlite")
            span.set_attribute("db.caller", caller)
            span.set_attribute("db.write.timeout_seconds", effective_timeout)
            try:
                lease.acquired = self._lock.acquire(timeout=effective_timeout)
                wait_ms = (time.perf_counter() - wait_start) * 1000
                span.set_attribute("db.write.wait_ms", round(wait_ms, 3))
                if not lease.acquired:
                    span.set_attribute("db.write.timed_out", True)
                    span.set_status(Status(StatusCode.ERROR, "write lock timeout"))
                    raise sqlite3.OperationalError(
                        f"WriteSerializer: timeout after {effective_timeout}s waiting for write lock"
                    )

                self._current_lease = lease
                hold_start = time.monotonic()
                try:
                    conn = self._conn
                    if conn is None:
                        raise sqlite3.OperationalError(
                            "WriteSerializer: connection already closed"
                        )
                    # A short observability deadline also bounds cross-process
                    # SQLite lock waits. The setting is restored before release.
                    if restore_busy_timeout:
                        conn.execute(f"PRAGMA busy_timeout = {lease.busy_timeout_ms}")
                    begin_retries = 1 if timeout is not None else 3
                    for attempt in range(begin_retries):
                        try:
                            conn.execute("BEGIN IMMEDIATE")
                            break
                        except sqlite3.OperationalError as begin_err:
                            if "database is locked" in str(begin_err).lower() and attempt < begin_retries - 1:
                                logger.warning(
                                    "BEGIN IMMEDIATE failed (attempt %d/%d), retrying in 2s",
                                    attempt + 1,
                                    begin_retries,
                                )
                                time.sleep(2)
                            else:
                                raise
                    yield conn
                    if lease.failed or not lease.acquired:
                        raise sqlite3.OperationalError(
                            "WriteSerializer: write scope lost its lease during external I/O"
                        )
                    try:
                        conn.execute("COMMIT")
                    except sqlite3.OperationalError as commit_err:
                        if "no transaction is active" not in str(commit_err).lower():
                            raise
                except BaseException as exc:
                    if isinstance(exc, sqlite3.OperationalError):
                        message = str(exc).lower()
                        if "database is locked" in message or "database is busy" in message:
                            span.set_attribute("db.write.timed_out", True)
                    if conn is not None and lease.acquired:
                        try:
                            conn.execute("ROLLBACK")
                        except Exception:
                            pass
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                    raise
                finally:
                    hold_ms = (time.monotonic() - hold_start) * 1000
                    span.set_attribute("db.write.hold_ms", round(hold_ms, 3))
                    if hold_ms > 500:
                        logger.warning(
                            "scoped_write held %.0fms [caller=%s]",
                            hold_ms,
                            caller,
                        )
                    if restore_busy_timeout and conn is not None and lease.acquired:
                        try:
                            conn.execute("PRAGMA busy_timeout = 30000")
                        except sqlite3.Error:
                            logger.debug("Failed to restore SQLite busy_timeout", exc_info=True)
            finally:
                if lease.acquired:
                    self._current_lease = None
                    lease.acquired = False
                    self._lock.release()

    def run_external_io(self, conn: sqlite3.Connection, fn):
        """Suspend an owned write lease for I/O, then start a fresh transaction.

        This legacy boundary commits before calling *fn*: it is not an atomic
        unit of work across I/O. A failed reacquisition invalidates this scope
        without touching the connection or lock now owned by another writer.
        """
        if conn is not self._conn:
            return fn()
        lease = self._current_lease
        if lease is None or not lease.acquired or lease.failed:
            raise sqlite3.ProgrammingError(
                "WriteSerializer: external I/O requires an active write scope"
            )
        try:
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if "no transaction is active" not in str(exc).lower():
                raise
        if lease.busy_timeout_ms is not None:
            conn.execute("PRAGMA busy_timeout = 30000")
        self._current_lease = None
        lease.acquired = False
        self._lock.release()
        try:
            return fn()
        finally:
            lease.acquired = self._lock.acquire(timeout=self._timeout)
            if not lease.acquired:
                lease.failed = True
                raise sqlite3.OperationalError(
                    f"WriteSerializer: timeout after {self._timeout}s reacquiring write lock"
                )
            self._current_lease = lease
            try:
                if conn is not self._conn:
                    raise sqlite3.OperationalError("WriteSerializer: connection already closed")
                if lease.busy_timeout_ms is not None:
                    conn.execute(f"PRAGMA busy_timeout = {lease.busy_timeout_ms}")
                conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                lease.failed = True
                raise

    def submit(self, fn, *, timeout: float | None = None):
        """Submit a write callable and optionally bound this acquisition."""
        with self.connection(timeout=timeout) as conn:
            return fn(conn)

    def close(self) -> None:
        """Close the underlying connection. Call on shutdown.

        Nils ``self._conn`` first so concurrent threads get a clean
        ``OperationalError`` instead of a C-level segfault from using a
        closed SQLite handle.
        """
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_write_serializer: WriteSerializer | None = None
_write_serializer_lock = threading.Lock()


def get_write_serializer() -> WriteSerializer:
    """Get or create the module-level WriteSerializer singleton."""
    global _write_serializer
    if _write_serializer is not None:
        return _write_serializer
    with _write_serializer_lock:
        if _write_serializer is None:
            from config.settings import DB_PATH
            _write_serializer = WriteSerializer(DB_PATH)
    return _write_serializer


def run_external_io(conn: sqlite3.Connection, fn):
    """Release the serializer lock while *fn* performs blocking external I/O."""
    serializer = get_write_pool()
    if isinstance(serializer, WriteSerializer):
        return serializer.run_external_io(conn, fn)
    return fn()


def run_external_io_with_receipt_scope(conn: sqlite3.Connection, fn):
    """Call ``fn`` without a serializer lease, passing its safe receipt connection.

    An owned serializer scope commits before I/O and is reacquired afterward;
    ``fn(None)`` must persist receipts through its own short scopes. Foreign
    caller-owned connections retain the legacy ``fn(conn)`` behavior. This is
    a transaction boundary, not a durable-delivery or exactly-once guarantee.
    """
    # A foreign/test connection must not initialize the production write pool.
    serializer = _write_serializer
    if isinstance(serializer, WriteSerializer) and conn is serializer._conn:
        return serializer.run_external_io(conn, lambda: fn(None))
    return fn(conn)


@contextmanager
def scoped_write(*, timeout: float | None = None) -> Iterator[sqlite3.Connection]:
    """Get a short-lived transactional connection for write operations."""
    # Use module-level lookup so test patches on get_write_pool work.
    serializer = globals()["get_write_pool"]()
    if timeout is None:
        scope = serializer.connection()
    else:
        try:
            scope = serializer.connection(timeout=timeout)
        except TypeError:
            # Compatibility for simple test doubles with a no-argument API.
            scope = serializer.connection()
    with scope as conn:
        yield conn


@contextmanager
def scoped_observability_write(
    *, timeout: float = 0.05,
) -> Iterator[sqlite3.Connection]:
    """Best-effort SQLite persistence with a short local deadline.

    This helper exists only for legacy trace/audit rows that must never wait on
    the main writer for the normal 60-second business-write timeout.
    """
    scoped = globals()["scoped_write"]
    try:
        scope = scoped(timeout=timeout)
    except TypeError:
        # Existing tests and integrations often monkeypatch a no-argument
        # scoped_write context manager. Preserve that patch surface.
        scope = scoped()
    with scope as conn:
        yield conn


# Deprecated shims (will be removed once all callers migrated)


# ── Deprecated shims (will be removed once all callers migrated) ────────


@contextmanager
def scoped_raw_write() -> Iterator[sqlite3.Connection]:
    """DEPRECATED — use scoped_write() instead.

    Temporarily kept so existing callers don't break during the
    WriteSerializer migration.  Delegates to scoped_write().
    """
    with scoped_write() as conn:
        yield conn


@contextmanager
def scoped_deferred() -> Iterator[sqlite3.Connection]:
    """DEPRECATED — use scoped_read() for reads, scoped_write() for writes.

    Temporarily kept so API deps.py doesn't break during migration.
    Delegates to scoped_write() with rollback safety.
    """
    with scoped_write() as conn:
        yield conn


def reset_deferred_pool() -> None:
    """DEPRECATED — no-op, kept for test fixture compatibility."""
    pass


def close_write_pool() -> None:
    """Shut down the write serializer. Call on application shutdown."""
    global _write_serializer
    if _write_serializer is not None:
        _write_serializer.close()
        _write_serializer = None


def reset_write_pool() -> None:
    """Reset the write serializer singleton (for tests).

    Does NOT close the old connection — background daemon threads spawned
    during tests may still hold references to it.  Closing it would cause
    a C-level segfault when those threads next call ``conn.execute()``.
    The old connection is GC'd once all references are dropped.

    Named ``reset_write_pool`` for backward compatibility with test fixtures.
    """
    global _write_serializer
    _write_serializer = None


# Backward-compat aliases used by a handful of callers
reset_write_serializer = reset_write_pool
get_write_pool = get_write_serializer
WritePool = WriteSerializer  # backward-compat for tests


# ── ReadPool: short-lived read-only connections ─────────────────────────


def _read_db_path() -> str:
    """Resolve the default DB path for read connections."""
    from config.settings import DB_PATH
    return str(DB_PATH)


_read_pool: list[tuple[sqlite3.Connection, str]] = []
_read_pool_lock = threading.Lock()
_READ_POOL_SIZE = 5


def reset_read_pool() -> None:
    """Close and discard all pooled read connections (for tests)."""
    with _read_pool_lock:
        for c, _path in _read_pool:
            try:
                c.close()
            except Exception:
                pass
        _read_pool.clear()


@contextmanager
def scoped_read() -> Iterator[sqlite3.Connection]:
    """Get a short-lived read-only connection.

    Usage::

        with scoped_read() as conn:
            rows = conn.execute("SELECT ...").fetchall()
        # connection returned to pool — never holds a write lock

    Under WAL mode, multiple readers can run concurrently with a single
    writer.  The ``query_only`` pragma prevents accidental writes.
    """
    db_path = _read_db_path()

    conn: sqlite3.Connection | None = None
    with _read_pool_lock:
        # Reuse a pooled connection ONLY if it targets the requested db_path.
        # A stale conn returned to the pool by a background thread from a prior
        # test (different db_path) must not be reused — it would query the
        # wrong database and surface as "no such table" flakiness (P2-12).
        for i, (c, path) in enumerate(_read_pool):
            if path == db_path:
                del _read_pool[i]
                conn = c
                break
    if conn is not None:
        try:
            conn.execute("SELECT 1")
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            conn = None  # fall through to create new
    if conn is None:
        conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        _configure_sqlite_connection(conn, write=False)
    try:
        yield conn
    finally:
        with _read_pool_lock:
            if len(_read_pool) < _READ_POOL_SIZE:
                _read_pool.append((conn, db_path))
            else:
                conn.close()
