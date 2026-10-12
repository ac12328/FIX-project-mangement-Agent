"""Handlers for canonical FIX session read tools."""
from __future__ import annotations

import sqlite3
from src.fix_sessions.repository import list_candidates, list_environment_resources, list_history, list_sessions


def _current(conn: sqlite3.Connection, args: dict) -> dict:
    project_id = args["project_id"].upper()
    sessions = list_sessions(conn, project_id, args.get("environment"))
    return {"sessions": sessions, "pending_count": len(list_candidates(conn, project_id))}


def _history(conn: sqlite3.Connection, args: dict) -> dict:
    return {"history": list_history(conn, args["project_id"].upper(), args["environment"], args.get("session_key", "order_entry"))}


def _pending(conn: sqlite3.Connection, args: dict) -> dict:
    project_id = args.get("project_id")
    return {"candidates": list_candidates(conn, project_id.upper() if project_id else None)}


def _resources(conn: sqlite3.Connection, args: dict) -> dict:
    return {"resources": list_environment_resources(conn, args["project_id"].upper(), args.get("environment"))}

FIX_SESSION_TOOL_HANDLERS = {
    "get_project_fix_sessions": _current,
    "get_fix_session_history": _history,
    "get_pending_fix_session_candidates": _pending,
    "get_fix_environment_resources": _resources,
}
