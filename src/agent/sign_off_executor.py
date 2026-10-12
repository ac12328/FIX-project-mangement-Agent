"""Deterministic read-only guidance for explicit project closing intent."""

from __future__ import annotations

import sqlite3

from src.agent.guards import (
    _sign_off_project_id,
    is_draft_request,
    is_inbound_email_context,
    is_production_sign_off,
    is_staged_update_confirm_intent,
)
from src.telegram.closing_advisory import build_closing_advisory


def try_deterministic_sign_off(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
) -> tuple[str, str] | None:
    """Return Dashboard-only closing guidance for explicit close intent.

    This path is intentionally read-only: it never stages an update, writes
    memory, pushes a confirmation card, or performs a terminal transition.
    """
    if not _should_run_sign_off(conn, user_text, chat_id):
        return None

    project_id = _sign_off_project_id(conn, user_text, chat_id=chat_id)
    if not project_id:
        return None
    return build_closing_advisory(conn, project_id), project_id


def _should_run_sign_off(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
) -> bool:
    """Return True only for explicit close / production sign-off intent."""
    if (
        is_inbound_email_context(user_text)
        or is_staged_update_confirm_intent(user_text)
        or is_draft_request(user_text)
    ):
        return False
    if not is_production_sign_off(user_text):
        return False
    return _sign_off_project_id(conn, user_text, chat_id=chat_id) is not None
