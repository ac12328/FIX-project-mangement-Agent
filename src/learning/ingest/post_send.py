"""Capture-only adapter for sent draft edit evidence."""
from __future__ import annotations
import sqlite3
from src.learning.ingest.base import capture_observation, sanitize_text
from src.learning.ingest.contracts import LearningObservation, ObservationSourceType, TrustTier

def capture_sent_draft_edit(conn: sqlite3.Connection, *, source_id: str, operator_id: str, artifact_id: str, before_text: str, after_text: str, project_id: str | None = None, contact_id: str | None = None, output_revision_id: str | None = None) -> LearningObservation:
    before_text, _ = sanitize_text(before_text)
    after_text, _ = sanitize_text(after_text)
    text = f"Before:\n{before_text}\nAfter:\n{after_text}"
    return capture_observation(conn, source_type=ObservationSourceType.DRAFT_EDIT, source_id=source_id, operator_id=operator_id, text=text, raw_payload={"change_kind": "sent_draft_edit", "before_span": {"start": len("Before:\n"), "end": len("Before:\n") + len(before_text)}, "after_span": {"start": len("Before:\n") + len(before_text) + len("\nAfter:\n"), "end": len(text)}}, trust_tier=TrustTier.OPERATOR, project_id=project_id, contact_id=contact_id, artifact_type="draft", artifact_id=artifact_id, output_revision_id=output_revision_id)
