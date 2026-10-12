"""Capture-only adapter for external client reply and outcome evidence."""
from __future__ import annotations
import sqlite3
from src.learning.ingest.base import capture_observation
from src.learning.ingest.contracts import LearningObservation, ObservationSourceType, TrustTier

def capture_client_reply(conn: sqlite3.Connection, *, source_id: str, operator_id: str, reply_text: str, project_id: str | None = None, contact_id: str | None = None, artifact_id: str | None = None, trace_id: str | None = None) -> LearningObservation:
    return capture_observation(conn, source_type=ObservationSourceType.CLIENT_REPLY, source_id=source_id, operator_id=operator_id, text=reply_text, raw_payload={"kind": "client_reply"}, trust_tier=TrustTier.EXTERNAL, project_id=project_id, contact_id=contact_id, artifact_type="email", artifact_id=artifact_id or source_id, trace_id=trace_id)
