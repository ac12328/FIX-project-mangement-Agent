"""Resolve operator_id for memory namespaces."""

from __future__ import annotations

import sqlite3

from config import defaults


def resolve_operator_id(
    conn: sqlite3.Connection | None = None,  # noqa: ARG001 — reserved for per-chat mapping
    *,
    chat_id: int | None = None,  # noqa: ARG001
) -> str:
    """Return configured operator namespace key."""
    op_id = getattr(defaults, "OPERATOR_ID", None) or "default_operator"
    return str(op_id).strip() or "default_operator"