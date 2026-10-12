"""Dynamic email poll batch limits when the operator is actively using Telegram."""

from __future__ import annotations

import sqlite3

OPERATOR_ACTIVE_WINDOW_SEC = 60

EMAIL_BATCH_LIMIT_IDLE = 30
EMAIL_BATCH_LIMIT_ACTIVE = 10
PERSIST_BATCH_IDLE = 10
PERSIST_BATCH_ACTIVE = 5


def resolve_email_poll_limits(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return (email_batch_limit, persist_batch) based on recent operator activity."""
    from src.repositories.operator_state_repo import seconds_since_last_telegram_interaction

    secs = seconds_since_last_telegram_interaction(conn)
    if secs is not None and secs <= OPERATOR_ACTIVE_WINDOW_SEC:
        return EMAIL_BATCH_LIMIT_ACTIVE, PERSIST_BATCH_ACTIVE
    return EMAIL_BATCH_LIMIT_IDLE, PERSIST_BATCH_IDLE