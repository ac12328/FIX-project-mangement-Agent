"""Session persistence: conversation summaries, progress docs, session summaries.

Extracted from agent_chat.py for the 500-line convention.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from config.settings import BASE_DIR
from src.db.connection import scoped_read, scoped_write
from src.llm.client import llm_call_structured

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_progress_prompt() -> str:
    return (BASE_DIR / "prompts" / "progress_update.md").read_text(encoding="utf-8")


_SUMMARY_MAX_TURNS = 20
_SUMMARY_EXPIRY_HOURS = 24
_SESSION_GAP_HOURS = 2


def _summary_excerpt(value: str, *, limit: int = 100) -> str:
    """Return one physical summary line from arbitrary user-visible text."""
    return " ".join(str(value or "").split())[:limit]


def _update_conversation_summary(
    conn: sqlite3.Connection, chat_id: int, user_text: str, agent_response: str,
) -> None:
    from src.repositories.working_memory_repo import upsert_memory, get_memory

    conn.execute(
        """INSERT OR IGNORE INTO projects (project_id, project_status, ball_with, current_milestone)
           VALUES ('_system', 'Archived', 'Internal', 'M1_NetworkSetup')""",
    )
    key = f"conversation_summary_{chat_id}"
    existing = get_memory(conn, "_system", key)
    existing_text = existing["memory_value"] if existing else ""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    new_entry = (
        f"[{ts}] User: {_summary_excerpt(user_text)}"
        f" -> Agent: {_summary_excerpt(agent_response)}"
    )
    lines = existing_text.split("\n") if existing_text else []
    lines.append(new_entry)
    lines = lines[-_SUMMARY_MAX_TURNS:]
    summary_text = "\n".join(lines)
    upsert_memory(conn, "_system", key, summary_text)

    from src.memory.reform_flags import operator_memory_enabled
    if operator_memory_enabled():
        from src.agent.operator_identity import resolve_operator_id
        from src.memory.operator_memory_store import OperatorMemoryStore
        from src.models.operator_memory import MemoryType

        store = OperatorMemoryStore(conn, resolve_operator_id(conn, chat_id=chat_id))
        store.put(
            scope="chat",
            scope_id=str(chat_id),
            memory_type=MemoryType.EPISODIC_SNIPPET,
            memory_key=f"conversation_summary_{chat_id}",
            memory_value={"description": summary_text, "text": summary_text},
            source="memory_flush",
        )


def _get_conversation_summary(conn: sqlite3.Connection, chat_id: int) -> str | None:
    from src.repositories.working_memory_repo import get_memory

    entry = get_memory(conn, "_system", f"conversation_summary_{chat_id}")
    if not entry or not entry["memory_value"]:
        return None

    raw = entry["memory_value"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_SUMMARY_EXPIRY_HOURS)
    gap_cutoff = now - timedelta(hours=_SESSION_GAP_HOURS)

    recent_lines: list[str] = []
    old_lines: list[str] = []

    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        ts = _parse_summary_ts(line)
        if ts is None:
            old_lines.append(line)
            continue
        if ts < cutoff:
            continue
        if ts < gap_cutoff:
            old_lines.append(line)
        else:
            recent_lines.append(line)

    parts: list[str] = []
    if old_lines:
        parts.append(
            "<previous-session>\n"
            + "\n".join(old_lines)
            + "\n</previous-session>"
        )
    if recent_lines:
        parts.append("\n".join(recent_lines))

    return "\n".join(parts) if parts else None


def _parse_summary_ts(line: str) -> datetime | None:
    """Extract datetime from a summary line like '[2026-04-06T15:07] ...'."""
    import re
    m = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\]", line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Structured progress document ──────────────────────────────────────

_PROGRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "integer"},
        "updated_at": {"type": "string"},
        "projects": {"type": "object"},
        "recent_decisions": {"type": "array", "items": {"type": "string"}},
        "pending": {"type": "array", "items": {"type": "string"}},
        "session_notes": {"type": "string"},
    },
    "required": ["version", "projects"],
}

_FALLBACK_LOG_MAX = 5
_PROGRESS_EXPIRY_DAYS = 7


def _update_progress(conn, chat_id, user_text, agent_response) -> None:
    """Update the structured progress document via LLM merge."""
    from src.repositories.working_memory_repo import upsert_memory, get_memory

    if conn is not None:
        existing = get_memory(conn, "_system", f"agent_progress_{chat_id}")
    else:
        try:
            with scoped_read() as rconn:
                existing = get_memory(rconn, "_system", f"agent_progress_{chat_id}")
        except Exception:
            existing = None

    current_json = existing["memory_value"] if existing else None

    try:
        current = json.loads(current_json) if current_json else {
            "version": 1, "updated_at": "", "projects": {},
            "recent_decisions": [], "pending": [], "session_notes": "",
        }
    except (json.JSONDecodeError, TypeError):
        current = {
            "version": 1, "updated_at": "", "projects": {},
            "recent_decisions": [], "pending": [], "session_notes": "",
        }

    user_msg = (
        f"Current progress document:\n```json\n{json.dumps(current, indent=2)}\n```\n\n"
        f"New exchange:\nUser: {user_text[:500]}\nAgent: {agent_response[:500]}"
    )

    key = f"agent_progress_{chat_id}"
    try:
        result = llm_call_structured(
            "summary_update",
            [
                {"role": "system", "content": _load_progress_prompt()},
                {"role": "user", "content": user_msg},
            ],
            _PROGRESS_SCHEMA,
            temperature=0.0,
        )
        if not isinstance(result, dict) or "projects" not in result:
            raise ValueError("LLM returned invalid progress structure")
        save_value = json.dumps(result, ensure_ascii=False)
    except Exception:
        logger.debug("Progress update LLM failed, using fallback", exc_info=True)
        fallback_log = current.get("_fallback_log", [])
        fallback_log.append({"user": user_text[:200], "agent": agent_response[:200]})
        current["_fallback_log"] = fallback_log[-_FALLBACK_LOG_MAX:]
        save_value = json.dumps(current, ensure_ascii=False)

    if conn is not None:
        conn.execute(
            """INSERT OR IGNORE INTO projects (project_id, project_status, ball_with, current_milestone)
               VALUES ('_system', 'Archived', 'Internal', 'M1_NetworkSetup')""",
        )
        upsert_memory(conn, "_system", key, save_value)
    else:
        with scoped_write() as wconn:
            wconn.execute(
                """INSERT OR IGNORE INTO projects (project_id, project_status, ball_with, current_milestone)
                   VALUES ('_system', 'Archived', 'Internal', 'M1_NetworkSetup')""",
            )
            upsert_memory(wconn, "_system", key, save_value)


def _get_progress(conn, chat_id) -> str | None:
    """Read the structured progress document and format for injection."""
    from src.repositories.working_memory_repo import get_memory

    entry = get_memory(conn, "_system", f"agent_progress_{chat_id}")
    if not entry or not entry["memory_value"]:
        return None

    try:
        progress = json.loads(entry["memory_value"])
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(progress, dict) or not progress.get("projects"):
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(days=_PROGRESS_EXPIRY_DAYS)).isoformat()
    active_projects = {}
    for pid, info in progress.get("projects", {}).items():
        touched = info.get("touched_at", "")
        if touched >= cutoff:
            active_projects[pid] = info

    if not active_projects and not progress.get("recent_decisions") and not progress.get("pending"):
        return None

    progress["projects"] = active_projects
    return _format_progress(progress)


def _format_progress(progress: dict) -> str:
    """Format progress JSON into a readable block for the LLM."""
    parts = []

    projects = progress.get("projects", {})
    if projects:
        parts.append("## Active projects")
        for pid, info in projects.items():
            action = info.get("last_action", "")
            state = info.get("state", "")
            parts.append(f"- **{pid}**: {action}. State: {state}.")

    decisions = progress.get("recent_decisions", [])
    if decisions:
        parts.append("\n## Recent decisions")
        for d in decisions:
            parts.append(f"- {d}")

    pending = progress.get("pending", [])
    if pending:
        parts.append("\n## Pending")
        for p in pending:
            parts.append(f"- {p}")

    notes = progress.get("session_notes", "")
    if notes:
        parts.append(f"\n## Session notes\n{notes}")

    return "\n".join(parts)


_SESSION_BOUNDARY_HOURS = 2


def _maybe_save_session_summary(
    conn: sqlite3.Connection | None,
    chat_id: int,
    user_text: str,
    agent_response: str,
) -> None:
    """Save a session summary if a meaningful conversation exists."""
    from src.repositories.session_summary_repo import save_session_summary
    from src.repositories.working_memory_repo import get_memory

    try:
        if conn is not None:
            entry = get_memory(conn, "_system", f"conversation_summary_{chat_id}")
        else:
            with scoped_read() as rconn:
                entry = get_memory(rconn, "_system", f"conversation_summary_{chat_id}")
        if not entry or not entry["memory_value"]:
            return

        raw = entry["memory_value"]
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        if len(lines) < 3:
            return

        result = llm_call_structured(
            "session_summary",
            [
                {"role": "system", "content": (
                    "Summarize this conversation log into a brief session summary. "
                    "Include: what was discussed, any decisions made, and open questions. "
                    "Return JSON with keys: summary, projects (list of IDs), "
                    "decisions (list), open_questions (list)."
                )},
                {"role": "user", "content": raw},
            ],
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "projects": {"type": "array", "items": {"type": "string"}},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"],
            },
            temperature=0.0,
        )

        if not isinstance(result, dict) or not result.get("summary"):
            return

        def _do_save(wconn):
            save_session_summary(
                wconn,
                chat_id=chat_id,
                summary_text=result["summary"],
                projects_discussed=", ".join(result.get("projects") or []) or None,
                decisions_made=", ".join(result.get("decisions") or []) or None,
                open_questions=", ".join(result.get("open_questions") or []) or None,
            )

        if conn is not None:
            _do_save(conn)
        else:
            with scoped_write() as wconn:
                _do_save(wconn)
    except Exception:
        logger.debug("Failed to save session summary", exc_info=True)


def _persist_summary(
    conn: sqlite3.Connection | None, chat_id: int, user_text: str, agent_response: str,
    *, skip_llm: bool = False,
) -> None:
    """Persist conversation summary and kick off background LLM summaries."""
    try:
        if conn is not None:
            _update_conversation_summary(conn, chat_id, user_text, agent_response)
        else:
            with scoped_write() as wconn:
                _update_conversation_summary(wconn, chat_id, user_text, agent_response)
    except Exception:
        logger.debug("Failed to persist conversation summary", exc_info=True)

    if skip_llm:
        return

    import threading

    def _background_summaries() -> None:
        try:
            _update_progress(None, chat_id, user_text, agent_response)
        except Exception:
            logger.debug("Background progress update failed", exc_info=True)
        try:
            _maybe_save_session_summary(None, chat_id, user_text, agent_response)
        except Exception:
            logger.debug("Background session summary failed", exc_info=True)

    threading.Thread(target=_background_summaries, daemon=True, name="persist-summary").start()
