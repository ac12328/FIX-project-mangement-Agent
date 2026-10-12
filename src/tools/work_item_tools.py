"""
Work item tool — list_work_items handler and tool contract.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from src.tools.contracts import ToolContract
from src.repositories.work_item_repo import list_active_work_items


LIST_WORK_ITEMS_TOOL = ToolContract(
    name="list_work_items",
    description=(
        "List work items across all types (projects, tickets, outreach, standalone). "
        "Use this instead of query_projects + search_tickets when you need a unified view "
        "of everything that needs attention. "
        "type='all' returns everything open; type='standalone' returns unclassified items."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["project", "support_ticket", "outreach", "standalone", "all"],
                "description": "Work item type filter. Default: all.",
            },
            "status": {
                "type": "string",
                "enum": ["open", "closed", "all"],
                "description": "Status filter. Default: open.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return. Default: 20.",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
    },
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    timeout_seconds=10,
)


def chat_list_work_items(conn: sqlite3.Connection, args: dict) -> dict:
    """Handler for list_work_items tool."""
    type_arg = (args.get("type") or "all").strip()
    status_arg = (args.get("status") or "open").strip()
    limit = int(args.get("limit") or 20)

    type_filter = None if type_arg == "all" else type_arg

    if status_arg == "all":
        rows = conn.execute(
            "SELECT * FROM work_items"
            + (" WHERE work_item_type = ?" if type_filter else "")
            + " ORDER BY updated_at DESC LIMIT ?",
            ([type_filter, limit] if type_filter else [limit]),
        ).fetchall()
        from src.models.work_item import WorkItem
        items = [WorkItem(**dict(r)) for r in rows]
    elif status_arg == "open":
        items = list_active_work_items(conn, type_filter=type_filter, limit=limit)
    else:
        rows = conn.execute(
            "SELECT * FROM work_items WHERE status = ?"
            + (" AND work_item_type = ?" if type_filter else "")
            + " ORDER BY updated_at DESC LIMIT ?",
            ([status_arg, type_filter, limit] if type_filter else [status_arg, limit]),
        ).fetchall()
        from src.models.work_item import WorkItem
        items = [WorkItem(**dict(r)) for r in rows]

    if not items:
        return {"text": "No work items found matching the filter."}

    lines = [f"Work items ({len(items)} found):"]
    for wi in items:
        entity_ref = f" [{wi.entity_table}/{wi.entity_id}]" if wi.entity_id else ""
        lines.append(
            f"  [{wi.work_item_id}] {wi.title[:60]}\n"
            f"    type={wi.work_item_type} | status={wi.status} | priority={wi.priority}"
            f"{entity_ref}"
        )
    return {"text": "\n".join(lines)}


WORK_ITEM_CHAT_TOOL_HANDLERS: dict[str, Any] = {
    "list_work_items": chat_list_work_items,
}
