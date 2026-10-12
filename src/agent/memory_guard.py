"""Cross-turn memory guard at tool execution boundary."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from src.agent.operator_identity import resolve_operator_id
from src.memory.operator_memory_store import OperatorMemoryStore

GUARDED_WRITE_TOOLS = frozenset({
    "update_project",
    "update_project_batch",
    "confirm_update",
})
_WRITE_TOOLS = GUARDED_WRITE_TOOLS


@dataclass(frozen=True)
class MemoryGuardResult:
    blocked: bool
    message: str | None = None
    matched_key: str | None = None


def _load_staged_confirm_fields(
    conn: sqlite3.Connection,
    project_id: str,
) -> dict[str, Any]:
    """Read pending_chat_update fields for confirm_update inspection."""
    from src.repositories.working_memory_repo import get_memory

    mem = get_memory(conn, project_id, "pending_chat_update")
    if not mem or not mem.get("memory_value"):
        return {}
    try:
        staged = json.loads(mem["memory_value"])
    except (json.JSONDecodeError, TypeError):
        return {}
    fields = staged.get("fields") if isinstance(staged, dict) else {}
    return dict(fields) if isinstance(fields, dict) else {}


def _extract_fields_from_args(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    if tool_name == "update_project":
        fields = tool_args.get("fields") or {}
        return dict(fields) if isinstance(fields, dict) else {}
    if tool_name == "update_project_batch":
        updates = tool_args.get("updates") or []
        merged: dict[str, Any] = {}
        if isinstance(updates, list):
            for item in updates:
                if isinstance(item, dict):
                    for k, v in (item.get("fields") or {}).items():
                        merged[k] = v
        return merged
    if tool_name == "confirm_update" and conn is not None and project_id:
        return _load_staged_confirm_fields(conn, project_id)
    return {}


def _fields_blocked(
    fields: dict[str, Any],
    blocked_fields: dict[str, list[str]],
) -> str | None:
    for field_name, blocked_values in blocked_fields.items():
        if field_name not in fields:
            continue
        val = fields[field_name]
        norm_blocked = {str(v).strip().lower() for v in blocked_values}
        if str(val).strip().lower() in norm_blocked:
            return field_name
    return None


def check_tool_call(
    conn: sqlite3.Connection | None,
    *,
    operator_id: str | None,
    tool_name: str,
    tool_args: dict[str, Any],
    project_id: str | None,
) -> MemoryGuardResult:
    """Return blocked=True when a stored forbidden rule applies."""
    if conn is None or tool_name not in _WRITE_TOOLS:
        return MemoryGuardResult(blocked=False)

    op_id = (
        operator_id
        if operator_id and operator_id not in ("*", "")
        else None
    )
    op_id = op_id or resolve_operator_id(conn)
    store = OperatorMemoryStore(conn, op_id)
    pid = project_id or tool_args.get("project_id")
    if not pid and tool_name == "update_project":
        pid = tool_args.get("project_id")

    candidates: list[dict] = store.list_forbidden_for_project(pid or "*")
    if not pid:
        candidates.extend(store.list_forbidden_for_project("*"))

    fields = _extract_fields_from_args(
        tool_name, tool_args, conn=conn, project_id=pid,
    )
    for row in candidates:
        if row.get("scope") == "project" and pid and row.get("scope_id") not in (pid, "*"):
            continue
        val = row.get("memory_value") or {}
        blocked_fields = val.get("blocked_fields") or {}
        if not blocked_fields and tool_name in _WRITE_TOOLS and row.get("memory_key", "").startswith("no_close"):
            blocked_fields = {"project_status": ["Closed", "Archived"]}
        hit = _fields_blocked(fields, blocked_fields)
        if hit:
            desc = val.get("description") or row.get("memory_key")
            return MemoryGuardResult(
                blocked=True,
                message=(
                    f"MemoryGuard: blocked '{tool_name}' on {hit} — stored rule: {desc}"
                ),
                matched_key=row.get("memory_key"),
            )
    return MemoryGuardResult(blocked=False)


def parse_tool_args(raw: str | dict) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}