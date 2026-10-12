"""Shared memory-reform wiring for agent loop callers."""

from __future__ import annotations

import sqlite3

from src.agent.operator_identity import resolve_operator_id
from src.memory.reform_flags import (
    memory_guard_enabled,
    operator_memory_enabled,
    thread_artifacts_enabled,
)


def resolve_loop_operator_id(
    conn: sqlite3.Connection | None,
    *,
    chat_id: int | None = None,
) -> str:
    return resolve_operator_id(conn, chat_id=chat_id)


def loop_memory_kwargs(
    conn: sqlite3.Connection | None,
    *,
    chat_id: int | None = None,
    session_id: str | None = None,
    operator_id: str | None = None,
) -> dict:
    """LoopConfig kwargs for memory guard + thread artifacts."""
    op_id = operator_id or resolve_loop_operator_id(conn, chat_id=chat_id)
    thread_id = session_id or (str(chat_id) if chat_id is not None else None)
    return {
        "memory_guard_enabled": memory_guard_enabled(),
        "operator_id": op_id,
        "thread_id": thread_id,
        "thread_artifacts_enabled": thread_artifacts_enabled(),
    }


def context_operator_id(
    conn: sqlite3.Connection | None,
    *,
    chat_id: int | None = None,
) -> str | None:
    from config import defaults

    governed_enabled = any((
        defaults.MEMORY_RETRIEVAL_V2_CHAT,
        defaults.MEMORY_RETRIEVAL_V2_WEB,
        defaults.MEMORY_RETRIEVAL_V2_DRAFT,
        defaults.MEMORY_RETRIEVAL_V2_ADVISORY,
        defaults.MEMORY_RETRIEVAL_V2_TOOL,
        defaults.MEMORY_RETRIEVER_SHADOW,
    ))
    if not operator_memory_enabled() and not governed_enabled:
        return None
    return resolve_loop_operator_id(conn, chat_id=chat_id)