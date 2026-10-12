"""Fail closed before an older binary modifies a newer database schema."""

from __future__ import annotations

import sqlite3

from config.defaults import CURRENT_SCHEMA_VERSION


def require_supported_schema_version(conn: sqlite3.Connection) -> int:
    """Read the released version without starting or committing a transaction."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = int(row[0]) if row and row[0] is not None else 0
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this application's "
            f"supported schema version {CURRENT_SCHEMA_VERSION}; refusing startup writes"
        )
    return current
