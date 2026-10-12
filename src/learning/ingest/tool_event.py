"""Capture-only adapter for tool failures and corrections."""
from __future__ import annotations
import sqlite3
from src.learning.ingest.base import capture_observation
from src.learning.ingest.contracts import LearningObservation, ObservationSourceType, TrustTier

def capture_tool_event(conn: sqlite3.Connection, *, source_id: str, operator_id: str, tool_name: str, event_kind: str, detail: str, project_id: str | None = None, contact_id: str | None = None, trace_id: str | None = None) -> LearningObservation:
    text = f"Tool {tool_name} {event_kind}: {detail}"
    return capture_observation(conn, source_type=ObservationSourceType.TOOL_FAILURE, source_id=source_id, operator_id=operator_id, text=text, raw_payload={"tool_name": tool_name, "event_kind": event_kind}, trust_tier=TrustTier.SYSTEM, project_id=project_id, contact_id=contact_id, artifact_type="tool_event", artifact_id=source_id, trace_id=trace_id)
