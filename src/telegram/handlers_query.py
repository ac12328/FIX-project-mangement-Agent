"""Handler for aggregate query clarification cards."""

from __future__ import annotations

import json
import sqlite3

from src.repositories.activity_log_repo import insert_activity
from src.telegram._cb_helpers import extract_link
from src.telegram.i18n import t
from src.telegram.session import cancel_session, complete_session, get_session, set_session_data


def handle_query_clarification(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    del project_id

    session_link = extract_link(message_context, "telegram_session")
    session_id = session_link["object_ref"] if session_link else None
    if not session_id:
        return {"text": t("query.no_session_context")}

    session = get_session(conn, session_id)
    if not session or session["status"] != "active":
        return {"text": t("query.session_expired"), "object_ref": session_id}
    if session["operator_id"] != operator_id:
        return {"_error": True, "text": "Session unavailable. No action was taken."}

    try:
        data = json.loads(session["intermediate_data"]) if session.get("intermediate_data") else {}
    except json.JSONDecodeError:
        data = {}

    query_text = data.get("query_text") or "query"

    if action == "CONFIRM_RUN":
        from src.portfolio.aggregate_query import execute_confirmed_query, format_query_result_text

        evaluation = execute_confirmed_query(
            conn,
            query_text=query_text,
            filters=data.get("filters") or None,
            search_text=data.get("search_text"),
            limit=int(data.get("limit") or 20),
        )
        insert_activity(
            conn,
            actor="operator",
            action_type="aggregate_query_confirmed",
            surface="telegram",
            object_ref=session_id,
            summary=f"Aggregate query confirmed: {query_text}",
        )
        conn.commit()
        complete_session(conn, session_id)
        return {"text": format_query_result_text(evaluation), "object_ref": session_id}

    if action == "MODIFY_FILTERS":
        data["awaiting_refined_query"] = True
        set_session_data(conn, session_id, data)
        insert_activity(
            conn,
            actor="operator",
            action_type="aggregate_query_modify_requested",
            surface="telegram",
            object_ref=session_id,
            summary=f"Aggregate query refinement requested: {query_text}",
        )
        conn.commit()
        return {
            "text": t("query.send_revised"),
            "session_id": session_id,
            "object_ref": session_id,
            "object_links": [{"object_type": "telegram_session", "object_ref": session_id}],
        }

    if action == "CANCEL":
        cancel_session(conn, session_id)
        insert_activity(
            conn,
            actor="operator",
            action_type="aggregate_query_cancelled",
            surface="telegram",
            object_ref=session_id,
            summary=f"Aggregate query cancelled: {query_text}",
        )
        conn.commit()
        return {"text": t("query.cancelled"), "object_ref": session_id}

    return {"text": t("query.unknown_action").format(action=action), "object_ref": session_id}
