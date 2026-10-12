"""Compatibility entry points for the unified durable learning worker."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from src.db.connection import scoped_write
from src.repositories.learning_outbox_repo import enqueue_learning


def make_turn_key(surface: str, conversation_id: str, messages: list[dict[str, Any]], *,
                  operator_id: str = "default_operator", project_id: str | None = None) -> str:
    last_user = next((str(msg.get("content") or "") for msg in reversed(messages) if msg.get("role") == "user"), "")
    digest = hashlib.sha256(last_user.encode("utf-8")).hexdigest()[:20]
    key = f"{surface}:{conversation_id}:{digest}"
    if operator_id != "default_operator" or project_id is not None:
        scope = json.dumps([operator_id, project_id], ensure_ascii=True, separators=(",", ":"))
        key += ":scope:" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
    return key


def capture_learning(
    conn: sqlite3.Connection | None,
    *,
    surface: str,
    conversation_id: str,
    messages: list[dict[str, Any]],
    round_count: int,
    tool_call_count: int,
    notify_chat_id: int | None = None,
    idempotency_key: str | None = None,
    operator_id: str = "default_operator",
    project_id: str | None = None,
) -> str | None:
    from src.learning.governance_flags import capture_enabled, outbox_v2_enabled
    if round_count < 2 or not capture_enabled():
        return None
    kwargs = dict(
        idempotency_key=idempotency_key or make_turn_key(surface, conversation_id, messages,
            operator_id=operator_id, project_id=project_id),
        surface=surface, conversation_id=conversation_id, messages=messages,
        round_count=round_count, tool_call_count=tool_call_count,
        notify_chat_id=notify_chat_id, enqueue=outbox_v2_enabled(),
        operator_id=operator_id, project_id=project_id,
    )
    if conn is not None:
        return enqueue_learning(conn, **kwargs)
    with scoped_write() as wconn:
        return enqueue_learning(wconn, **kwargs)


def process_learning_outbox(*, limit: int = 10) -> dict[str, int]:
    from src.learning.governance_flags import worker_disabled
    from src.learning.jobs.worker import LearningJobWorker
    if worker_disabled():
        return {"claimed": 0, "completed": 0, "failed": 0}
    result = LearningJobWorker().run_once(limit=limit)
    return {
        "claimed": result.claimed,
        "completed": result.completed + result.quarantined,
        "failed": result.retried + result.dead_lettered,
    }
