"""Capture-only adapter for operator feedback."""
from __future__ import annotations
import sqlite3
from src.learning.ingest.base import capture_observation
from src.learning.ingest.contracts import LearningObservation, ObservationSourceType, TrustTier

def capture_operator_feedback(conn: sqlite3.Connection, *, source_id: str, operator_id: str, feedback: str, project_id: str | None = None, contact_id: str | None = None, trace_id: str | None = None, output_revision_id: str | None = None) -> LearningObservation:
    return capture_observation(conn, source_type=ObservationSourceType.OPERATOR_FEEDBACK, source_id=source_id, operator_id=operator_id, text=feedback, raw_payload={"kind": "operator_feedback"}, trust_tier=TrustTier.OPERATOR, project_id=project_id, contact_id=contact_id, artifact_type="feedback", artifact_id=source_id, trace_id=trace_id, output_revision_id=output_revision_id)
