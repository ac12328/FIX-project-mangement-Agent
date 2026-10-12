"""Capture-only adapter for conversation-flush evidence."""
from __future__ import annotations

import sqlite3
from typing import Any

from src.learning.ingest.base import capture_observation, sanitize_text
from src.learning.ingest.contracts import LearningObservation, ObservationSourceType, TrustTier


def capture_conversation_flush(conn: sqlite3.Connection, *, source_id: str, operator_id: str, messages: list[dict[str, Any]], project_id: str | None = None, trace_id: str | None = None) -> LearningObservation:
    parts, spans = [], []
    offset = 0
    for item in messages:
        role = str(item.get("role", "unknown"))
        content, _ = sanitize_text(str(item.get("content", "")))
        prefix = f"{role}: "
        parts.append(prefix + content)
        spans.append({"role": role, "start": offset + len(prefix), "end": offset + len(prefix) + len(content)})
        offset += len(prefix) + len(content) + 1
    text = "\n".join(parts)
    return capture_observation(conn, source_type=ObservationSourceType.CONVERSATION_FLUSH, source_id=source_id, operator_id=operator_id, text=text, raw_payload={"message_count": len(messages), "roles": [str(item.get("role", "unknown")) for item in messages], "message_spans": spans}, trust_tier=TrustTier.OPERATOR, project_id=project_id, artifact_type="conversation", artifact_id=source_id, trace_id=trace_id)
