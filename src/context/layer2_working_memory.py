"""
Context Layer 2: Working Memory loader.
Loads recent working memory entries, flags stale ones.
"""

import sqlite3

from src.models.context_layers import WorkingMemoryContext
from src.repositories.working_memory_repo import get_all_memory


# Keys that map to specific WorkingMemoryContext fields
_KEY_MAP = {
    "recent_thread_summary": "recent_thread_summary",
    "latest_operator_instruction": "latest_operator_instruction",
}
_LIST_KEYS = {
    "pending_question": "pending_questions",
    "open_decision": "open_decisions",
}


def load_working_memory(
    conn: sqlite3.Connection, project_id: str
) -> tuple[WorkingMemoryContext, list[str]]:
    """
    Load working memory for a project.
    Returns (WorkingMemoryContext, missing_flags).
    """
    entries = get_all_memory(conn, project_id)
    missing: list[str] = []

    if not entries:
        missing.append("no_working_memory")
        return WorkingMemoryContext(), missing

    ctx = WorkingMemoryContext()
    stale_keys = []

    for entry in entries:
        key = entry.get("memory_key", "")
        value = entry.get("memory_value", "")
        is_stale = entry.get("is_stale", False)

        if is_stale:
            stale_keys.append(key)

        if key in _KEY_MAP:
            setattr(ctx, _KEY_MAP[key], value)
        elif key.startswith("pending_question"):
            ctx.pending_questions.append(value)
        elif key.startswith("open_decision"):
            ctx.open_decisions.append(value)

    ctx.stale_keys = stale_keys
    return ctx, missing
