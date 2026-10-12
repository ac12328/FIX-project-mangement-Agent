"""Agent tools for personal tasks, commitments, waiting-for items, and reminders."""

from __future__ import annotations

import sqlite3
from typing import Any

from config import defaults
from config.settings import CONFIG
from src.db.savepoint import savepoint
from src.repositories.personal_work_item_repo import (
    create_personal_work_item,
    list_personal_work_items,
)
from src.services.personal_work_item_service import change_personal_work_item
from src.scheduling.service import create_one_shot_reminder
from src.tools.contracts import ToolContract


CREATE_PERSONAL_WORK_ITEM = ToolContract(
    name="create_personal_work_item",
    description="Create a task, commitment, waiting-for item, or reminder for the operator.",
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["task", "commitment", "waiting_for", "reminder"]},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "due_at": {"type": "string"},
            "remind_at": {"type": "string"},
            "related_entity_type": {"type": "string"},
            "related_entity_id": {"type": "string"},
        },
        "required": ["kind", "title"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    audit_required=True,
)

LIST_PERSONAL_WORK_ITEMS = ToolContract(
    name="list_personal_work_items",
    description="List personal tasks, commitments, waiting-for items, and reminders.",
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "status": {"type": "string"},
            "review_status": {"type": "string"},
        },
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
)

MANAGE_PERSONAL_WORK_ITEM = ToolContract(
    name="manage_personal_work_item",
    description="Complete, cancel, or snooze a personal work item.",
    input_schema={
        "type": "object",
        "properties": {
            "work_item_id": {"type": "string"},
            "action": {"type": "string", "enum": ["complete", "cancel", "snooze"]},
            "snoozed_until": {"type": "string"},
            "resolution_note": {"type": "string"},
        },
        "required": ["work_item_id", "action"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    audit_required=True,
)


def _enabled() -> bool:
    return defaults.PERSONAL_ASSISTANT_ENABLED and defaults.PERSONAL_ASSISTANT_WORK_ITEMS_ENABLED


def create_handler(conn: sqlite3.Connection, args: dict) -> dict:
    if not _enabled():
        return {"error": "Personal work items are disabled"}
    try:
        if args["kind"] == "reminder":
            if not (
                defaults.PERSONAL_ASSISTANT_REMINDERS_ENABLED
                and args.get("remind_at")
            ):
                return {"error": "Reminder scheduling is disabled or remind_at is missing"}
            return create_one_shot_reminder(
                conn,
                operator_id=CONFIG.operator_id,
                title=args["title"],
                remind_at=args["remind_at"],
                related_entity_type=args.get("related_entity_type"),
                related_entity_id=args.get("related_entity_id"),
                source_type="agent",
            )
        with savepoint(conn, ensure_transaction=True):
            item = create_personal_work_item(
                conn,
                operator_id=CONFIG.operator_id,
                kind=args["kind"],
                title=args["title"],
                description=args.get("description"),
                priority=args.get("priority") or "medium",
                due_at=args.get("due_at"),
                remind_at=args.get("remind_at"),
                related_entity_type=args.get("related_entity_type"),
                related_entity_id=args.get("related_entity_id"),
                source_type="agent",
                commit=False,
            )
    except ValueError as exc:
        return {"error": str(exc)}
    return item.model_dump()


def list_handler(conn: sqlite3.Connection, args: dict) -> dict:
    if not _enabled():
        return {"error": "Personal work items are disabled"}
    items = list_personal_work_items(
        conn,
        CONFIG.operator_id,
        kind=args.get("kind"),
        status=args.get("status", "open"),
        review_status=args.get("review_status"),
    )
    return {"items": [item.model_dump() for item in items]}


def manage_handler(conn: sqlite3.Connection, args: dict) -> dict:
    if not _enabled():
        return {"error": "Personal work items are disabled"}
    action = args["action"]
    work_item_id = args["work_item_id"]
    if action == "snooze" and not args.get("snoozed_until"):
        return {"error": "snoozed_until is required for snooze"}
    item = change_personal_work_item(
        conn,
        operator_id=CONFIG.operator_id,
        work_item_id=work_item_id,
        action=action,
        snoozed_until=args.get("snoozed_until"),
        resolution_note=args.get("resolution_note"),
    )
    return {"success": item is not None, "work_item_id": work_item_id, "action": action}


PERSONAL_WORK_TOOL_HANDLERS: dict[str, Any] = {
    "create_personal_work_item": create_handler,
    "list_personal_work_items": list_handler,
    "manage_personal_work_item": manage_handler,
}

PERSONAL_WORK_TOOLS = [
    CREATE_PERSONAL_WORK_ITEM,
    LIST_PERSONAL_WORK_ITEMS,
    MANAGE_PERSONAL_WORK_ITEM,
]
