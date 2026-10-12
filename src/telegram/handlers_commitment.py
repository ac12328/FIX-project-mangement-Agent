"""Handle Profile Q commitment cards against the card's exact session."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable

from src.db.savepoint import savepoint
from src.email.commitment_extractor import create_followup_from_commitment
from src.repositories.activity_log_repo import insert_activity
from src.telegram._cb_helpers import extract_message_metadata
from src.telegram.operator_binding import fallback_operator_id
from src.telegram.session import complete_session, get_session

logger = logging.getLogger(__name__)

_UNAVAILABLE = "Commitment card unavailable. No action was taken."


class _UnavailableSession(Exception):
    """A stale or foreign commitment card must have no write effects."""


def _card_session(
    conn: sqlite3.Connection,
    project_id: str | None,
    message_context: dict | None,
    operator_id: str,
) -> tuple[str, dict] | None:
    """Revalidate the pinned card/session inside the mutation's write scope."""
    metadata = extract_message_metadata(message_context)
    if not project_id or not message_context or (
        message_context.get("project_id") != project_id
        or metadata.get("card_profile") != "Q"
        or metadata.get("project_id") != project_id
    ):
        return None
    session_id = metadata.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    session = get_session(conn, session_id)
    if not session or session["status"] != "active" or (
        session["flow_type"] != "commitment_confirm"
        or session["originating_ref"] != project_id
        or session["operator_id"] not in {operator_id, fallback_operator_id()}
    ):
        return None
    try:
        data = json.loads(session.get("intermediate_data") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("project_id") != project_id:
        return None
    return session_id, data


def handle_commitment(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
    post_commit: list[Callable[[], None]] | None = None,
) -> dict:
    """Confirm or dismiss exactly once, atomically with the session resolution.

    A callback supplies a post-commit collector and owns the surrounding write
    transaction. Direct callers retain the previous commit-on-success contract.
    """
    if action not in {"CONFIRM", "DISMISS"}:
        return {"_error": True, "text": f"Unknown commitment action: {action}"}

    try:
        with savepoint(conn, ensure_transaction=True):
            selected = _card_session(conn, project_id, message_context, operator_id)
            if selected is None:
                raise _UnavailableSession
            session_id, data = selected
            if action == "CONFIRM":
                commitment = {
                    "has_commitment": True,
                    "commitment_text": data.get("commitment_text") or "Client commitment",
                    "due_date": data.get("due_date") or "",
                }
                followup_id = create_followup_from_commitment(
                    conn, project_id, commitment, commit=False,
                )
                if not followup_id:
                    raise ValueError("Commitment follow-up was not created")
                insert_activity(
                    conn,
                    actor=operator_id,
                    action_type="commitment_confirmed",
                    project_id=project_id,
                    surface="telegram",
                    summary=f"Confirmed commitment follow-up: {commitment['commitment_text'][:80]}",
                )
                text = (
                    f"Follow-up created for {project_id}: "
                    f"{commitment['commitment_text'][:60]}"
                )
            else:
                insert_activity(
                    conn,
                    actor=operator_id,
                    action_type="commitment_dismissed",
                    project_id=project_id,
                    surface="telegram",
                    summary=f"Dismissed commitment: {str(data.get('commitment_text') or 'unknown')[:80]}",
                )
                text = f"Commitment dismissed for {project_id}."
            if not complete_session(conn, session_id, commit=False):
                raise ValueError("Commitment session changed during confirmation")
        if post_commit is None:
            conn.commit()
        return {"text": text, "project_id": project_id}
    except _UnavailableSession:
        return {"_error": True, "text": _UNAVAILABLE}
    except Exception:
        logger.exception("Commitment card command failed: %s", project_id)
        return {"_error": True, "text": "Commitment outcome could not be confirmed. Check the follow-up before retrying."}
