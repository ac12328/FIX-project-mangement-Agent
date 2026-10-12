"""Internal action chat handler functions — runtime execution for internal_actions tools."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.repositories.internal_action_repo import (
    insert_internal_action,
    list_internal_actions,
    complete_internal_action,
    cancel_internal_action,
)


def chat_create_internal_action(conn: sqlite3.Connection, args: dict) -> dict:
    title = (args.get("title") or "").strip()
    owner_team = (args.get("owner_team") or "").strip()
    if not title or not owner_team:
        return {"error": "title and owner_team are required"}

    action = insert_internal_action(
        conn,
        project_id=args.get("project_id") or None,
        owner_team=owner_team,
        title=title,
        deadline=args.get("deadline") or None,
    )
    return {
        "action_id": action.action_id,
        "owner_team": action.owner_team,
        "title": action.title,
        "deadline": action.deadline,
        "status": action.status,
        "created_at": action.created_at,
    }


def chat_list_internal_actions(conn: sqlite3.Connection, args: dict) -> dict:
    project_id = args.get("project_id") or None
    status = args.get("status") or "open"
    actions = list_internal_actions(conn, project_id=project_id, status=status)
    return {
        "actions": [
            {
                "action_id": a.action_id,
                "owner_team": a.owner_team,
                "title": a.title,
                "deadline": a.deadline,
                "status": a.status,
                "project_id": a.project_id,
                "created_at": a.created_at,
            }
            for a in actions
        ],
        "count": len(actions),
    }


def chat_complete_internal_action(conn: sqlite3.Connection, args: dict) -> dict:
    action_id = (args.get("action_id") or "").strip()
    outcome = (args.get("outcome") or "done").strip()
    if not action_id:
        return {"error": "action_id is required"}

    if outcome == "done":
        action = complete_internal_action(conn, action_id)
    elif outcome == "cancelled":
        action = cancel_internal_action(conn, action_id)
    else:
        return {"error": f"Invalid outcome '{outcome}'. Must be 'done' or 'cancelled'"}

    if action is None:
        return {"error": f"Action '{action_id}' not found"}

    return {
        "action_id": action.action_id,
        "status": action.status,
        "completed_at": action.completed_at,
    }


INTERNAL_ACTION_TOOL_HANDLERS: dict[str, Any] = {
    "create_internal_action": chat_create_internal_action,
    "list_internal_actions": chat_list_internal_actions,
    "complete_internal_action": chat_complete_internal_action,
}
