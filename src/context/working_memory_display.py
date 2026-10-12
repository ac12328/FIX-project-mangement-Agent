"""
Operator-facing working memory: filter internal keys and enrich staleness.

Root cause addressed: latest_note is only updated via store_note(); project truth
writes do not refresh it, so UI must surface time + semantic staleness and hide
system undo/pending keys.
"""

from __future__ import annotations

import re
import sqlite3

from src.repositories.project_repo import get_project_field
from src.repositories.working_memory_repo import get_all_memory

# Exact keys never shown on draft/workbench surfaces
_OPERATOR_MEMORY_DENY_KEYS: frozenset[str] = frozenset({
    "undo_auto_apply",
    "undo_auto_apply_update_id",
})

# Prefixes for session/agent/system scratch (not operator context)
_OPERATOR_MEMORY_DENY_PREFIXES: tuple[str, ...] = (
    "undo_",
    "undo_auto_apply:",
    "pending_update_",
    "error_retry:",
    "agent_progress_",
    "review_rejected_",
)


_MILESTONE_NUM_RE = re.compile(r"\bM(\d+)(?:_|[^\d]|$)", re.IGNORECASE)


def _milestone_index(milestone: str | None) -> int | None:
    if not milestone:
        return None
    m = re.match(r"M(\d+)", milestone.strip(), re.IGNORECASE)
    return int(m.group(1)) if m else None


def _max_milestone_mentioned_in_text(text: str) -> int | None:
    nums = [int(n) for n in _MILESTONE_NUM_RE.findall(text)]
    return max(nums) if nums else None


def milestone_supersedes_note(note_text: str, current_milestone: str | None) -> bool:
    """True when note cites a milestone behind the project's current milestone."""
    current_idx = _milestone_index(current_milestone)
    if current_idx is None:
        return False
    note_idx = _max_milestone_mentioned_in_text(note_text)
    if note_idx is None:
        return False
    return note_idx < current_idx


def is_operator_visible_memory_key(memory_key: str) -> bool:
    if memory_key in _OPERATOR_MEMORY_DENY_KEYS:
        return False
    return not any(memory_key.startswith(p) for p in _OPERATOR_MEMORY_DENY_PREFIXES)


def enrich_memory_entry(
    entry: dict,
    *,
    current_milestone: str | None,
) -> dict:
    """Add operator-facing stale flags without mutating stored values."""
    out = dict(entry)
    time_stale = bool(out.get("is_stale"))
    stale_reason: str | None = None
    semantic = False

    if out.get("memory_key") == "latest_note" and out.get("memory_value"):
        if milestone_supersedes_note(str(out["memory_value"]), current_milestone):
            semantic = True
            stale_reason = "milestone_superseded"

    if time_stale and stale_reason is None:
        stale_reason = "age"

    out["is_stale"] = time_stale or semantic
    out["stale_reason"] = stale_reason
    return out


def get_operator_working_memory(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[dict]:
    """Memory rows safe for dashboard draft/workbench, sorted by updated_at desc."""
    current_ms = get_project_field(conn, project_id, "current_milestone")
    rows = get_all_memory(conn, project_id)
    visible = [r for r in rows if is_operator_visible_memory_key(r.get("memory_key", ""))]
    enriched = [enrich_memory_entry(r, current_milestone=current_ms) for r in visible]
    enriched.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return enriched