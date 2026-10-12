"""Tool handlers for learning loop — agent_memory and session_search."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.repositories.agent_notes_repo import (
    CHAR_LIMIT,
    add_note,
    list_notes,
    remove_note,
    replace_note,
    total_chars,
)
from src.repositories.session_summary_repo import search_sessions


def _remember_to_operator_store(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    *,
    memory_type: str,
    default_key_prefix: str,
) -> dict:
    from src.agent.operator_identity import resolve_operator_id
    from src.memory.operator_memory_store import OperatorMemoryStore
    from src.models.operator_memory import MemoryType

    scope = args.get("scope", "project")
    project_id = args.get("project_id", "")
    from src.models.learning_receipt import LearningWriteReceipt
    if scope == "project" and not project_id:
        return LearningWriteReceipt(
            success=False, operation=f"remember_{memory_type}",
            text="'project_id' is required when scope=project.", code="invalid_arguments",
        ).model_dump()

    rule_key = args.get("key") or args.get("rule_key") or f"{default_key_prefix}_{project_id or 'global'}"
    description = args.get("value") or args.get("description") or ""
    if not description:
        return LearningWriteReceipt(
            success=False, operation=f"remember_{memory_type}",
            text="'value' or 'description' is required.", code="invalid_arguments",
        ).model_dump()

    scope_id = project_id if scope == "project" else "*"
    if scope == "chat":
        chat_id = args.get("chat_id")
        if not chat_id:
            return LearningWriteReceipt(
                success=False, operation=f"remember_{memory_type}",
                text="'chat_id' is required when scope=chat.", code="invalid_arguments",
            ).model_dump()
        scope_id = str(chat_id)

    blocked_fields = args.get("blocked_fields")
    if memory_type == "forbidden" and not blocked_fields:
        blocked_fields = {"project_status": ["Closed", "Archived", "Completed"]}

    from src.models.learning_receipt import LearningWriteReceipt

    operator_id = resolve_operator_id(conn)
    store = OperatorMemoryStore(conn, operator_id)
    persisted_scope = "project" if memory_type == "forbidden" else (
        scope if scope in ("global", "project", "chat", "counterparty") else "global"
    )
    persisted_scope_id = project_id or "*" if memory_type == "forbidden" else scope_id
    persisted_type = MemoryType.FORBIDDEN.value if memory_type == "forbidden" else memory_type
    try:
        if memory_type == "forbidden":
            store.put_forbidden(
                project_id=project_id or "*",
                rule_key=rule_key,
                description=description,
                blocked_fields=blocked_fields,
                source="operator",
            )
        else:
            store.put(
                scope=persisted_scope,
                scope_id=persisted_scope_id,
                memory_type=MemoryType(memory_type),
                memory_key=rule_key,
                memory_value={"description": description, "text": description},
                source="operator",
            )
        conn.commit()
        from src.repositories.operator_memory_repo import get_memory
        row = get_memory(
            conn, operator_id, persisted_scope, persisted_scope_id, persisted_type, rule_key,
        )
        expected = (
            {"description": description, "blocked_fields": blocked_fields or {}}
            if memory_type == "forbidden"
            else {"description": description, "text": description}
        )
        verified = bool(row and row.get("memory_value") == expected)
        return LearningWriteReceipt(
            success=verified,
            verified=verified,
            operation=f"remember_{memory_type}",
            record_id=str(row["id"]) if verified else None,
            version=row["updated_at"] if verified else None,
            text=f"Stored operator memory [{memory_type}] {persisted_scope}/{persisted_scope_id}: {rule_key}",
            code=None if verified else "not_persisted",
        ).model_dump()
    except Exception:
        return LearningWriteReceipt(
            success=False, operation=f"remember_{memory_type}",
            text="Failed to store operator memory.", code="write_failed",
        ).model_dump()


def _blocked_learning_receipt(action: str) -> dict | None:
    """Return a typed denial for every LLM-exposed active-memory mutation."""
    from src.learning.governance_flags import candidate_only, writes_disabled
    from src.models.learning_receipt import LearningWriteReceipt

    if not (writes_disabled() or candidate_only()):
        return None
    code = "learning_writes_disabled" if writes_disabled() else "learning_candidate_only"
    return LearningWriteReceipt(
        success=False,
        operation=action,
        text="Learning writes are disabled." if writes_disabled() else "Capture-only learning mode is enabled.",
        code=code,
    ).model_dump()


def _note_receipt(
    conn: sqlite3.Connection,
    *,
    action: str,
    key: str,
    value: str = "",
    note_type: str | None = None,
) -> dict:
    """Mutate legacy notes and return a read-after-write verified receipt."""
    from src.models.learning_receipt import LearningWriteReceipt

    if blocked := _blocked_learning_receipt(action):
        return blocked
    if action == "add":
        existing = conn.execute("SELECT id FROM agent_notes WHERE note_key=?", (key,)).fetchone()
        if existing:
            return LearningWriteReceipt(
                success=False, operation=action, text=f"Key '{key}' already exists.",
                code="already_exists",
            ).model_dump()
    if action == "add":
        text = add_note(conn, key, value, note_type=note_type or "knowledge")
        row = conn.execute(
            "SELECT id, note_value, note_type, updated_at FROM agent_notes WHERE note_key=?", (key,)
        ).fetchone()
        verified = bool(row and row["note_value"] == value and row["note_type"] == (note_type or "knowledge"))
        return LearningWriteReceipt(
            success=verified, verified=verified, operation=action,
            record_id=str(row["id"]) if verified else None,
            version=row["updated_at"] if verified else None, text=text,
            code=None if verified else "not_persisted",
        ).model_dump()
    if action == "replace":
        text = replace_note(conn, key, value, note_type=note_type)
        row = conn.execute(
            "SELECT id, note_value, note_type, updated_at FROM agent_notes WHERE note_key=?", (key,)
        ).fetchone()
        expected_type = note_type or (row["note_type"] if row else None)
        verified = bool(row and row["note_value"] == value and row["note_type"] == expected_type)
        return LearningWriteReceipt(
            success=verified, verified=verified, operation=action,
            record_id=str(row["id"]) if verified else None,
            version=row["updated_at"] if verified else None, text=text,
            code=None if verified else "not_persisted",
        ).model_dump()
    before = conn.execute(
        "SELECT id, updated_at FROM agent_notes WHERE note_key=?", (key,)
    ).fetchone()
    text = remove_note(conn, key)
    row = conn.execute("SELECT id FROM agent_notes WHERE note_key=?", (key,)).fetchone()
    verified = bool(before and row is None and "Removed note" in text)
    return LearningWriteReceipt(
        success=verified, verified=verified, operation=action,
        record_id=str(before["id"]) if verified else None,
        version=before["updated_at"] if verified else None,
        text=text, code=None if verified else "not_persisted",
    ).model_dump()


def handle_agent_memory(conn: sqlite3.Connection, args: dict[str, Any]) -> dict:
    """LLM-curated personal memory — add/replace/remove/list notes."""
    action = args.get("action", "")
    key = args.get("key", "")
    value = args.get("value", "")

    if action == "remember_forbidden":
        if blocked := _blocked_learning_receipt(action):
            return blocked
        return _remember_to_operator_store(
            conn, args, memory_type="forbidden", default_key_prefix="no_close",
        )

    if action == "remember_preference":
        if blocked := _blocked_learning_receipt(action):
            return blocked
        return _remember_to_operator_store(
            conn, args, memory_type="semantic", default_key_prefix="preference",
        )

    if action == "list":
        note_type = args.get("note_type")
        notes = list_notes(conn, note_type=note_type)
        total = total_chars(conn)
        if not notes:
            filter_msg = f" (type={note_type})" if note_type else ""
            return {"text": f"No agent notes{filter_msg} yet. (0/{CHAR_LIMIT} chars)"}
        lines = [f"- **{n['note_key']}** [{n['note_type']}]: {n['note_value']}" for n in notes]
        header = f"Agent notes ({total}/{CHAR_LIMIT} chars)"
        if note_type:
            header += f" [filtered: {note_type}]"
        return {"text": header + ":\n" + "\n".join(lines)}

    if action == "add":
        if not key or not value:
            return {"error": "Both 'key' and 'value' are required for add."}
        return _note_receipt(
            conn, action=action, key=key, value=value,
            note_type=args.get("note_type", "knowledge"),
        )

    if action == "replace":
        if not key or not value:
            return {"error": "Both 'key' and 'value' are required for replace."}
        return _note_receipt(
            conn, action=action, key=key, value=value, note_type=args.get("note_type"),
        )

    if action == "remove":
        if not key:
            return {"error": "'key' is required for remove."}
        return _note_receipt(conn, action=action, key=key)

    return {
        "error": (
            f"Unknown action '{action}'. Use: list, add, replace, remove, "
            "remember_forbidden, remember_preference."
        ),
    }


def handle_session_search(conn: sqlite3.Connection, args: dict[str, Any]) -> dict:
    """FTS5 search over past session summaries."""
    query = args.get("query", "").strip()
    if not query:
        return {"error": "'query' is required."}

    results = search_sessions(conn, query, limit=5)
    if not results:
        return {"text": "No matching past sessions found."}

    lines = []
    for r in results:
        lines.append(f"**{r['started_at'] or 'Unknown date'}** — {r['snippet']}")
        if r.get("projects_discussed"):
            lines.append(f"  Projects: {r['projects_discussed']}")
        if r.get("decisions_made"):
            lines.append(f"  Decisions: {r['decisions_made']}")

    text = "\n".join(lines)
    if len(text) > 2000:
        text = text[:1997] + "..."
    return {"text": text}


LEARNING_TOOL_HANDLERS: dict[str, Any] = {
    "agent_memory": handle_agent_memory,
    "session_search": handle_session_search,
}
