"""Capture-only adapters for manual evidence and Wiki ingestion."""
from __future__ import annotations
import sqlite3
from src.learning.ingest.base import capture_observation
from src.learning.ingest.contracts import LearningObservation, ObservationSourceType, TrustTier

def capture_manual_memory(conn: sqlite3.Connection, *, source_id: str, operator_id: str, text: str, project_id: str | None = None, contact_id: str | None = None) -> LearningObservation:
    return capture_observation(conn, source_type=ObservationSourceType.MANUAL_MEMORY, source_id=source_id, operator_id=operator_id, text=text, raw_payload={"kind": "manual_memory"}, trust_tier=TrustTier.OPERATOR, project_id=project_id, contact_id=contact_id, artifact_type="manual_note", artifact_id=source_id)

def capture_wiki_ingestion(conn: sqlite3.Connection, *, source_id: str, operator_id: str, page_path: str, text: str, project_id: str | None = None) -> LearningObservation:
    return capture_observation(conn, source_type=ObservationSourceType.WIKI_INGESTION, source_id=source_id, operator_id=operator_id, text=text, raw_payload={"page_path": page_path}, trust_tier=TrustTier.EXTERNAL, project_id=project_id, artifact_type="wiki_page", artifact_id=page_path)

def capture_migration(conn: sqlite3.Connection, *, source_id: str, operator_id: str, text: str, legacy_ref: str) -> LearningObservation:
    return capture_observation(conn, source_type=ObservationSourceType.MIGRATION, source_id=source_id, operator_id=operator_id, text=text, raw_payload={"legacy_ref": legacy_ref}, trust_tier=TrustTier.SYSTEM, artifact_type="legacy_record", artifact_id=legacy_ref)
