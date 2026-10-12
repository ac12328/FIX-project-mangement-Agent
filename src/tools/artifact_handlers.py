"""Handlers for thread artifact retrieval."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def chat_read_artifact(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    artifact_id = (args.get("artifact_id") or "").strip()
    if not artifact_id:
        return {"error": "artifact_id is required"}
    from src.memory.thread_artifacts import get_artifact

    row = get_artifact(conn, artifact_id)
    if not row:
        return {"error": f"artifact '{artifact_id}' not found"}
    content = row.get("content_json") or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = content
    return {
        "artifact_id": artifact_id,
        "tool_name": row.get("tool_name"),
        "project_id": row.get("project_id"),
        "content": parsed,
        "chars": row.get("char_count"),
    }


ARTIFACT_TOOL_HANDLERS: dict[str, Any] = {
    "read_artifact": chat_read_artifact,
}